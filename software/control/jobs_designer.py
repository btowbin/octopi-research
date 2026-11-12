"""Job designer: write a job, test it against a simulated microscope, see what it would do, get help.

Opened from the JOBs tab ("Job designer...") or on its own:   python -m control.jobs_designer [job.py]

  * editor with colours, completion, hover help and live checking (typos, unknown channels, wrong arguments),
  * Test (F5): runs the job with simulated time and no hardware (a 2-day job takes a moment) and shows
      - the Plan: timeline per position, stage map, working time per time point, images per time point,
        hover for details, click an image in the plan to jump to the line of the code that makes it,
      - Output: summary (images, autofocus runs, duration, late time points), warnings, print() output and scope.log() messages,
  * Help panel: every function with description and example (Insert button), guide pages, Python basics, examples, snippets.

Nothing here moves the stage or switches on light. "Use in JOBs tab" hands the saved file to the JOBs tab; the job is
started there.
"""
import ast
import csv
import ctypes
import html
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import xml.etree.ElementTree as ET

from qtpy.QtCore import Qt, QTimer, Signal, QSettings, QSize
from qtpy.QtGui import QColor, QKeySequence, QTextCursor, QTextDocument, QFont, QFontDatabase
from qtpy.QtWidgets import (QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
                            QTabWidget, QTextBrowser, QTextEdit, QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QListWidget,
                            QListWidgetItem, QComboBox, QDockWidget, QAction, QFileDialog, QMessageBox, QTableWidget,
                            QTableWidgetItem, QDoubleSpinBox, QSpinBox, QGroupBox, QFormLayout, QDialog, QDialogButtonBox,
                            QScrollArea, QToolBar, QCheckBox, QMenu, QHeaderView, QAbstractItemView, QSizePolicy, QStyle)

from control.jobs import apidoc
from control.jobs.api import make_points, SimClock, JobError
from control.jobs.dryrun import DryRunBackend
from control.jobs.lint import lint, summary as lint_summary
from control.jobs.runner import run_job, describe, DRY_RUN_MAX_EVENTS
from control.jobs_editor import CodeEditor
from control.jobs_designer_views import PlanView, StageMapView, BudgetView, MatrixView, fmt_t, fmt_dur

ORG, APP = 'TowbinLab', 'SquidJobDesigner'
DEFAULT_CHANNELS = {
    'BF LED matrix full': dict(exposure_ms=12.0, gain=0.0, intensity=3.0),
    'DF LED matrix': dict(exposure_ms=22.0, gain=0.0, intensity=5.0),
    'Fluorescence 405 nm Ex': dict(exposure_ms=100.0, gain=10.0, intensity=100.0),
    'Fluorescence 488 nm Ex': dict(exposure_ms=100.0, gain=10.0, intensity=100.0),
    'Fluorescence 561 nm Ex': dict(exposure_ms=100.0, gain=10.0, intensity=100.0),
}
TEST_TIMEOUT_S = 30.0


# ------------------------------------------------------------------------------------------------
# helpers (no Qt)
# ------------------------------------------------------------------------------------------------
def load_channel_file(path):
    """channel_configurations.xml -> {name: dict(exposure_ms, gain, intensity)} (empty dict if unreadable)"""
    try:
        out = {}
        for m in ET.parse(path).getroot().iter('mode'):
            name = m.get('Name')
            if name and 'RGB' not in name and 'Spectrometer' not in name:
                out[name] = dict(exposure_ms=float(m.get('ExposureTime', 0)), gain=float(m.get('AnalogGain', 0)),
                                 intensity=float(m.get('IlluminationIntensity', 0)))
        return out
    except Exception:
        return {}


def parse_positions(text):
    """CSV text 'id, x_mm, y_mm[, z_mm]' (one position per line; # comments; header line allowed; separators , ; or tab)
    -> (ids, coords). Raises ValueError('line N: ...')."""
    ids, coords = [], []
    first = True
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r'[,;\t]', line)]
        if first:
            first = False
            try:
                float(parts[1])
            except (ValueError, IndexError):
                continue                      # header
        if len(parts) < 3:
            raise ValueError(f'line {n}: expected  id, x, y[, z]  but found "{raw.strip()}"')
        try:
            x, y = float(parts[1]), float(parts[2])
            z = float(parts[3]) if len(parts) > 3 and parts[3] not in ('', 'None') else None
        except ValueError:
            raise ValueError(f'line {n}: x, y and z must be numbers: "{raw.strip()}"')
        ids.append(parts[0])
        coords.append((x, y) if z is None else (x, y, z))
    return ids, coords


def positions_to_text(ids, coords):
    lines = ['id, x_mm, y_mm, z_mm']
    for pid, c in zip(ids, coords):
        z = c[2] if len(c) > 2 and c[2] is not None else ''
        lines.append(f'{pid}, {c[0]:.4f}, {c[1]:.4f}, {z if z == "" else f"{z:.5f}"}')
    return '\n'.join(lines)


def sample_positions(groups, per_group, spacing=1.0):
    ids, coords = [], []
    for gi, g in enumerate(groups):
        for i in range(per_group):
            ids.append(f'{g}-{i}' if g else '')
            coords.append((10.0 + i * spacing, 10.0 + gi * 2 * spacing, 1.0))
    return ids, coords


def source_title(path_or_text, is_text=False):
    """first line of the module docstring (no execution)"""
    try:
        src = path_or_text if is_text else open(path_or_text, encoding='utf-8').read()
        doc = ast.get_docstring(ast.parse(src)) or ''
        return doc.strip().splitlines()[0] if doc.strip() else ''
    except Exception:
        return ''


class _AbortableDryRun(DryRunBackend):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.abort = False

    def abort_requested(self):
        return self.abort


class TestRun:
    """One dry run in a worker thread (a job with an endless loop must not freeze the designer)."""

    def __init__(self, path, points, infos, timing):
        self.path, self.points, self.infos, self.timing = path, points, infos, timing
        self.result = None
        self.messages = []
        self.stdout = io.StringIO()
        self.crash = None
        self.done = False
        self.started = time.time()
        self.clock = SimClock()
        self.backend = _AbortableDryRun(infos, clock=self.clock, **timing)
        self.thread = threading.Thread(target=self._work, daemon=True)

    def start(self):
        self.thread.start()

    def _work(self):
        old = sys.stdout
        sys.stdout = self.stdout
        try:
            self.result = run_job(self.path, self.points, self.backend, experiment_dir=None, clock=self.clock, save_images=False,
                                  max_events=DRY_RUN_MAX_EVENTS, record=True, trace_file=self.path)
            self.messages = list(self.backend.messages)
        except BaseException as e:        # the runner itself failed: report, never crash the designer
            self.crash = f'{e!r}\n{traceback.format_exc()}'
        finally:
            sys.stdout = old
            self.done = True

    def cancel(self):
        self.backend.abort = True

    def kill(self):
        """last resort for a job stuck in pure Python code (no scope call): raise an exception inside the thread"""
        try:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.thread.ident), ctypes.py_object(KeyboardInterrupt))
        except Exception:
            pass


# ------------------------------------------------------------------------------------------------
# Test setup panel: positions, channels, timing used by the test
# ------------------------------------------------------------------------------------------------
class SampleDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Sample positions')
        form = QFormLayout(self)
        self.groups = QLineEdit('A, B')
        self.groups.setToolTip('Group names, separated by commas. Leave empty for positions without ID. Position IDs become A-0, A-1, ...')
        self.n = QSpinBox(); self.n.setRange(1, 500); self.n.setValue(6)
        self.spacing = QDoubleSpinBox(); self.spacing.setRange(0.01, 100); self.spacing.setValue(1.0); self.spacing.setSuffix(' mm')
        form.addRow('Groups', self.groups)
        form.addRow('Positions per group', self.n)
        form.addRow('Spacing', self.spacing)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self):
        groups = [g.strip() for g in self.groups.text().split(',')]
        groups = [g for g in groups if g] or ['']
        return groups, self.n.value(), self.spacing.value()


class TestSetupPanel(QWidget):
    changed = Signal()

    def __init__(self, channels_provider=None, positions_provider=None, parent=None):
        super().__init__(parent)
        self.channels_provider = channels_provider
        self.positions_provider = positions_provider
        self._loading = False
        tabs = QTabWidget()
        # positions
        pos = QWidget()
        pl = QVBoxLayout(pos)
        self.pos_text = QPlainTextEdit()
        self.pos_text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.pos_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.pos_text.setToolTip('One position per line:  id, x_mm, y_mm, z_mm   (the group is the part of the id before the first "-")')
        self.pos_status = QLabel('')
        self.pos_status.setWordWrap(True)
        row = QHBoxLayout()
        self.btn_from_main = QPushButton('Load from Flexible Multipoint')
        self.btn_from_main.setToolTip('Copy the positions of the Flexible Multipoint tab (as they are now)')
        self.btn_from_main.setEnabled(positions_provider is not None)
        self.btn_sample = QPushButton('Sample positions...')
        self.btn_sample.setToolTip('Make up positions: some groups with a number of positions each')
        self.btn_csv = QPushButton('Open CSV...')
        for b in (self.btn_from_main, self.btn_sample, self.btn_csv):
            row.addWidget(b)
        row.addStretch(1)
        note = QLabel('The test uses these positions instead of the real stage positions. IDs define the groups (part before the first "-").')
        note.setWordWrap(True)
        note.setStyleSheet('color: gray')
        pl.addWidget(note); pl.addLayout(row); pl.addWidget(self.pos_text, 1); pl.addWidget(self.pos_status)
        tabs.addTab(pos, 'Positions')
        # channels
        ch = QWidget()
        cl = QVBoxLayout(ch)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Channel', 'Exposure (ms)', 'Gain', 'Intensity'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setToolTip('Channels of the channel configuration. Double click a number to try other values: this changes only the test, not the microscope.')
        row2 = QHBoxLayout()
        self.btn_reload_ch = QPushButton('Reload from microscope settings')
        self.btn_reload_ch.setEnabled(channels_provider is not None)
        row2.addWidget(self.btn_reload_ch); row2.addStretch(1)
        note2 = QLabel('Exposure, gain and intensity can be changed here to see how the timing changes. This affects only the test. Double click a name to insert it into the job.')
        note2.setWordWrap(True)
        note2.setStyleSheet('color: gray')
        cl.addWidget(note2); cl.addWidget(self.table, 1); cl.addLayout(row2)
        tabs.addTab(ch, 'Channels')
        # timing
        tm = QWidget()
        tl = QFormLayout(tm)
        self.sp_move = QDoubleSpinBox(); self.sp_move.setRange(0, 600); self.sp_move.setValue(1.0); self.sp_move.setSuffix(' s'); self.sp_move.setDecimals(2)
        self.sp_cap = QDoubleSpinBox(); self.sp_cap.setRange(0, 60); self.sp_cap.setValue(0.4); self.sp_cap.setSuffix(' s'); self.sp_cap.setDecimals(2)
        self.sp_af = QDoubleSpinBox(); self.sp_af.setRange(0, 600); self.sp_af.setValue(6.0); self.sp_af.setSuffix(' s'); self.sp_af.setDecimals(1)
        tl.addRow('One stage move', self.sp_move)
        tl.addRow('Overhead per image (besides exposure)', self.sp_cap)
        tl.addRow('One autofocus', self.sp_af)
        hint = QLabel('Assumed durations for the test. Measure them on your microscope (the log of a real job shows the times) and enter them here for a better estimate of whether the interval is long enough.')
        hint.setWordWrap(True); hint.setStyleSheet('color: gray')
        tl.addRow(hint)
        tabs.addTab(tm, 'Timing')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(tabs)
        self.tabs = tabs
        # signals
        self.pos_text.textChanged.connect(self._pos_changed)
        self.table.itemChanged.connect(lambda *_: None if self._loading else self.changed.emit())
        for sp in (self.sp_move, self.sp_cap, self.sp_af):
            sp.valueChanged.connect(lambda *_: self.changed.emit())
        self.btn_from_main.clicked.connect(self.load_from_main)
        self.btn_sample.clicked.connect(self.make_sample)
        self.btn_csv.clicked.connect(self.open_csv)
        self.btn_reload_ch.clicked.connect(self.load_channels)
        self.load_channels()
        if not self.load_from_main(quiet=True):
            self.set_positions_text(positions_to_text(*sample_positions(['A', 'B'], 4)))
        self._pos_changed()

    # --- positions
    def set_positions_text(self, text):
        self.pos_text.setPlainText(text)

    def load_from_main(self, quiet=False):
        if self.positions_provider is None:
            return False
        try:
            ids, coords = self.positions_provider()
        except Exception as e:
            if not quiet:
                QMessageBox.warning(self, 'Positions', f'Could not read the positions: {e}')
            return False
        if not len(ids):
            if not quiet:
                QMessageBox.information(self, 'Positions', 'The Flexible Multipoint tab has no positions yet.')
            return False
        self.set_positions_text(positions_to_text(ids, coords))
        return True

    def make_sample(self):
        d = SampleDialog(self)
        if d.exec_():
            self.set_positions_text(positions_to_text(*sample_positions(*d.values())))

    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open positions', '', 'CSV files (*.csv *.txt);;All files (*)')
        if path:
            try:
                self.set_positions_text(open(path, encoding='utf-8').read())
            except Exception as e:
                QMessageBox.warning(self, 'Positions', str(e))

    def _pos_changed(self):
        try:
            ids, coords = parse_positions(self.pos_text.toPlainText())
            groups = {}
            for p in make_points(ids, coords):
                groups[p.group] = groups.get(p.group, 0) + 1
            self.pos_status.setText(f'{len(ids)} positions; groups: ' + (', '.join(f'{g} ({n})' for g, n in groups.items()) or '-'))
            self.pos_status.setStyleSheet('color: gray')
        except ValueError as e:
            self.pos_status.setText(str(e))
            self.pos_status.setStyleSheet('color: #c0392b')
        self.changed.emit()

    def points(self):
        """list of Point; raises ValueError with a readable message"""
        ids, coords = parse_positions(self.pos_text.toPlainText())
        if not ids:
            raise ValueError('There are no positions. Add some in the Positions tab (or "Sample positions...").')
        return make_points(ids, coords)

    def group_names(self):
        try:
            return list(dict.fromkeys(p.group for p in self.points()))
        except ValueError:
            return []

    # --- channels
    def load_channels(self):
        infos = None
        if self.channels_provider is not None:
            try:
                infos = self.channels_provider()
            except Exception:
                infos = None
        self.set_channels(infos or DEFAULT_CHANNELS)

    def set_channels(self, infos):
        self._loading = True
        self.table.setRowCount(0)
        for name, i in infos.items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = QTableWidgetItem(name)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, it)
            for c, key in enumerate(('exposure_ms', 'gain', 'intensity'), 1):
                v = QTableWidgetItem(f'{float(i.get(key, 0) or 0):g}')
                v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, v)
        self._loading = False
        self.changed.emit()

    def channel_infos(self):
        out = {}
        for r in range(self.table.rowCount()):
            name = self.table.item(r, 0).text()
            vals = []
            for c in (1, 2, 3):
                try:
                    vals.append(float(self.table.item(r, c).text()))
                except ValueError:
                    vals.append(0.0)
            out[name] = dict(exposure_ms=vals[0], gain=vals[1], intensity=vals[2])
        return out

    def timing(self):
        return dict(move_s=self.sp_move.value(), capture_overhead_s=self.sp_cap.value(), autofocus_s=self.sp_af.value())


# ------------------------------------------------------------------------------------------------
# Help panel
# ------------------------------------------------------------------------------------------------
class HelpPanel(QTabWidget):
    insert_requested = Signal(str)
    open_example_requested = Signal(str)

    def __init__(self, example_dir=None, parent=None):
        super().__init__(parent)
        self.example_dir = example_dir
        self._build_functions()
        self._build_guide()
        self._build_examples()
        self._build_snippets()

    # --- functions
    def _build_functions(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search the functions (e.g. autofocus, interval, channel) ...')
        self.search.setClearButtonEnabled(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        row = QHBoxLayout()
        self.btn_ins_call = QPushButton('Insert call')
        self.btn_ins_call.setToolTip('Insert the function with its arguments at the cursor in the editor')
        self.btn_ins_ex = QPushButton('Insert example')
        self.btn_ins_ex.setToolTip('Insert the example code at the cursor in the editor')
        row.addWidget(self.btn_ins_call); row.addWidget(self.btn_ins_ex); row.addStretch(1)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tree)
        box = QWidget(); bl = QVBoxLayout(box); bl.setContentsMargins(0, 0, 0, 0); bl.addWidget(self.detail, 1); bl.addLayout(row)
        split.addWidget(box)
        split.setSizes([210, 360])
        lay.addWidget(self.search)
        lay.addWidget(split, 1)
        self._entries = {}
        self._populate_tree()
        self.search.textChanged.connect(self._filter)
        self.tree.currentItemChanged.connect(self._tree_selected)
        self.btn_ins_call.clicked.connect(self._insert_call)
        self.btn_ins_ex.clicked.connect(self._insert_example)
        self.tree.itemDoubleClicked.connect(lambda *_: self._insert_call())
        self.addTab(w, 'Functions')
        self._select_first()

    def _populate_tree(self):
        self.tree.clear()
        self._items = {}
        cats = {}
        for cat in apidoc.CATEGORIES:
            top = QTreeWidgetItem([cat])
            f = top.font(0); f.setBold(True); top.setFont(0, f)
            self.tree.addTopLevelItem(top)
            cats[cat] = top
        for e in apidoc.API:
            args = e['signature'].split(e['name'], 1)[1] if e['kind'] == 'method' else ''
            it = QTreeWidgetItem([f'{e["name"]}{args}'])
            it.setToolTip(0, e['summary'])
            it.setData(0, Qt.UserRole, ('api', e['name']))
            cats[e['category']].addChild(it)
            self._items[e['name']] = it
        top = QTreeWidgetItem(['Objects'])
        f = top.font(0); f.setBold(True); top.setFont(0, f)
        self.tree.addTopLevelItem(top)
        for o in apidoc.OBJECTS:
            it = QTreeWidgetItem([o['name']])
            it.setData(0, Qt.UserRole, ('object', o['name']))
            top.addChild(it)
            self._items[o['name']] = it
        self.tree.expandAll()

    def _filter(self, text):
        q = text.strip()
        hits = {e['name'] for e in apidoc.search(q)} if q else None
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            vis = 0
            for j in range(top.childCount()):
                ch = top.child(j)
                kind, name = ch.data(0, Qt.UserRole)
                show = hits is None or (kind == 'api' and name in hits) or (kind == 'object' and all(w in self._object_text(name) for w in q.lower().split()))
                ch.setHidden(not show)
                vis += show
            top.setHidden(vis == 0)
        if q:
            for name in (hits or []):
                self.tree.setCurrentItem(self._items[name])
                break

    @staticmethod
    def _object_text(name):
        o = next(o for o in apidoc.OBJECTS if o['name'] == name)
        return (o['name'] + ' ' + o['description'] + ' ' + ' '.join(a + ' ' + d for a, t, d in o['attrs'])).lower()

    def _select_first(self):
        self.tree.setCurrentItem(self._items['capture'])

    def _tree_selected(self, item, _prev=None):
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        kind, name = data
        if kind == 'api':
            e = apidoc.api_by_name()[name]
            self.detail.setHtml(apidoc.entry_html(e))
            self.btn_ins_call.setEnabled(True)
            self.btn_ins_ex.setEnabled(bool(e.get('example')))
        else:
            o = next(o for o in apidoc.OBJECTS if o['name'] == name)
            self.detail.setHtml(apidoc.object_html(o))
            self.btn_ins_call.setEnabled(False)
            self.btn_ins_ex.setEnabled(False)

    def _current_entry(self):
        it = self.tree.currentItem()
        data = it.data(0, Qt.UserRole) if it else None
        if data and data[0] == 'api':
            return apidoc.api_by_name()[data[1]]
        return None

    def _insert_call(self):
        e = self._current_entry()
        if e:
            if e['kind'] == 'method':
                args = e['signature'].split(e['name'], 1)[1]
                self.insert_requested.emit(f'scope.{e["name"]}{args}')
            else:
                self.insert_requested.emit(f'scope.{e["name"]}')

    def _insert_example(self):
        e = self._current_entry()
        if e and e.get('example'):
            self.insert_requested.emit(e['example'] + '\n')

    def show_function(self, name):
        if name in self._items:
            self.search.clear()
            self._filter('')
            self.tree.setCurrentItem(self._items[name])
            self.tree.scrollToItem(self._items[name])
            self.setCurrentIndex(0)
            return True
        return False

    # --- guide
    def _build_guide(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.topic_box = QComboBox()
        self.topic_box.addItems(list(apidoc.TOPICS))
        self.guide = QTextBrowser()
        lay.addWidget(self.topic_box)
        lay.addWidget(self.guide, 1)
        self.topic_box.currentTextChanged.connect(lambda t: self.guide.setHtml(apidoc.TOPICS.get(t, '')))
        self.guide.setHtml(apidoc.TOPICS[self.topic_box.currentText()])
        self.addTab(w, 'Guide')

    def show_topic(self, title):
        if title in apidoc.TOPICS:
            self.topic_box.setCurrentText(title)
            self.setCurrentWidget(self.topic_box.parentWidget())

    # --- examples
    def _build_examples(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.ex_list = QListWidget()
        self.ex_view = QTextBrowser()
        self.btn_open_example = QPushButton('Open a copy in the editor')
        self.btn_open_example.setToolTip('Start a new job from this example (it is not changed; save your copy under a new name)')
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.ex_list)
        box = QWidget(); bl = QVBoxLayout(box); bl.setContentsMargins(0, 0, 0, 0); bl.addWidget(self.ex_view, 1); bl.addWidget(self.btn_open_example)
        split.addWidget(box)
        split.setSizes([140, 440])
        lay.addWidget(split)
        self.refresh_examples()
        self.ex_list.currentRowChanged.connect(self._example_selected)
        self.ex_list.itemDoubleClicked.connect(lambda *_: self._open_example())
        self.btn_open_example.clicked.connect(self._open_example)
        self.addTab(w, 'Examples')

    def refresh_examples(self):
        self.ex_list.clear()
        self.ex_paths = []
        d = self.example_dir
        if d and os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith('.py') and not f.startswith('_'):
                    path = os.path.join(d, f)
                    self.ex_paths.append(path)
                    t = source_title(path)
                    self.ex_list.addItem(f'{f}' + (f'  -  {t}' if t else ''))
        if self.ex_paths:
            self.ex_list.setCurrentRow(0)
        else:
            self.ex_view.setHtml('<p>No examples found' + (f' in {html.escape(str(d))}' if d else '') + '.</p>')

    def _example_selected(self, row):
        if not 0 <= row < len(self.ex_paths):
            return
        path = self.ex_paths[row]
        try:
            src = open(path, encoding='utf-8').read()
            tree = ast.parse(src)
            title = ast.get_docstring(tree) or ''
            fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_job'), None)
            doc = (ast.get_docstring(fn) if fn else '') or ''
        except Exception as e:
            self.ex_view.setHtml(f'<p>Could not read the example: {html.escape(str(e))}</p>')
            return
        h = apidoc.CSS + f'<h3>{html.escape(os.path.basename(path))}</h3><p><b>{html.escape(title)}</b></p>'
        if doc:
            h += f'<pre style="white-space:pre-wrap">{html.escape(doc)}</pre>'
        h += '<h3>Code</h3><pre>' + html.escape(src) + '</pre>'
        self.ex_view.setHtml(h)

    def _open_example(self):
        row = self.ex_list.currentRow()
        if 0 <= row < len(self.ex_paths):
            self.open_example_requested.emit(self.ex_paths[row])

    # --- snippets
    def _build_snippets(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.sn_list = QListWidget()
        for title, desc, code in apidoc.SNIPPETS:
            it = QListWidgetItem(title)
            it.setToolTip(desc)
            self.sn_list.addItem(it)
        self.sn_desc = QLabel()
        self.sn_desc.setWordWrap(True)
        self.sn_code = QPlainTextEdit()
        self.sn_code.setReadOnly(True)
        self.sn_code.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.btn_insert_snip = QPushButton('Insert at cursor')
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.sn_list)
        box = QWidget(); bl = QVBoxLayout(box); bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.sn_desc); bl.addWidget(self.sn_code, 1); bl.addWidget(self.btn_insert_snip)
        split.addWidget(box)
        split.setSizes([200, 360])
        lay.addWidget(split)
        self.sn_list.currentRowChanged.connect(self._snippet_selected)
        self.sn_list.itemDoubleClicked.connect(lambda *_: self._insert_snippet())
        self.btn_insert_snip.clicked.connect(self._insert_snippet)
        self.sn_list.setCurrentRow(0)
        self.addTab(w, 'Snippets')

    def _snippet_selected(self, row):
        if 0 <= row < len(apidoc.SNIPPETS):
            self.sn_desc.setText(apidoc.SNIPPETS[row][1])
            self.sn_code.setPlainText(apidoc.SNIPPETS[row][2])

    def _insert_snippet(self):
        row = self.sn_list.currentRow()
        if 0 <= row < len(apidoc.SNIPPETS):
            self.insert_requested.emit(apidoc.SNIPPETS[row][2])


# ------------------------------------------------------------------------------------------------
class FindBar(QWidget):
    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText('Find')
        self.edit.setClearButtonEnabled(True)
        self.btn_next = QPushButton('Next'); self.btn_prev = QPushButton('Previous'); self.btn_close = QPushButton('x')
        self.btn_close.setFixedWidth(24)
        self.count = QLabel('')
        for wdg in (QLabel('Find'), self.edit, self.btn_prev, self.btn_next, self.count, self.btn_close):
            lay.addWidget(wdg)
        lay.setStretch(1, 1)
        self.edit.returnPressed.connect(self.find_next)
        self.edit.textChanged.connect(self._text_changed)
        self.btn_next.clicked.connect(self.find_next)
        self.btn_prev.clicked.connect(lambda: self.find_next(backward=True))
        self.btn_close.clicked.connect(self.close_bar)
        self.hide()

    def open_bar(self):
        c = self.editor.textCursor()
        if c.hasSelection():
            self.edit.setText(c.selectedText())
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()
        self._text_changed()

    def close_bar(self):
        self.hide()
        self.editor.set_find_matches([])
        self.editor.setFocus()

    def _text_changed(self):
        text = self.edit.text()
        matches = []
        if text:
            doc = self.editor.document()
            cur = doc.find(text, 0)
            while not cur.isNull() and len(matches) < 2000:
                matches.append(cur)
                cur = doc.find(text, cur)
        self.editor.set_find_matches(matches)
        self.count.setText(f'{len(matches)} found' if text else '')

    def find_next(self, backward=False):
        text = self.edit.text()
        if not text:
            return
        flags = QTextDocument.FindBackward if backward else QTextDocument.FindFlags()
        if not self.editor.find(text, flags):
            c = self.editor.textCursor()
            c.movePosition(QTextCursor.End if backward else QTextCursor.Start)
            self.editor.setTextCursor(c)
            self.editor.find(text, flags)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.close_bar()
        else:
            super().keyPressEvent(ev)


# ------------------------------------------------------------------------------------------------
class JobDesignerWindow(QMainWindow):
    """The designer window.

    channels_provider()   -> {channel name: dict(exposure_ms, gain, intensity)}   (None: channel_configurations.xml / defaults)
    positions_provider()  -> (ids, coords)                                        (None: sample positions)
    use_job_callback(path) hands a saved job to the JOBs tab; may return a text that is shown in the status bar
    """

    def __init__(self, channels_provider=None, positions_provider=None, job_path=None, example_dir=None, use_job_callback=None,
                 parent=None, use_settings=True):
        super().__init__(parent)
        self.setWindowTitle('Job designer')
        self.channels_provider = channels_provider
        self.use_job_callback = use_job_callback
        self.path = None
        self.example_dir = example_dir if example_dir is not None else self._find_example_dir()
        self.settings = QSettings(ORG, APP) if use_settings else None
        self.last_dir = self._setting('last_dir', os.path.dirname(job_path) if job_path else (self.example_dir or ''))
        self.test_run = None
        self.last_result = None
        self.last_points = []
        self._closing = False
        if channels_provider is None:
            for cand in ('channel_configurations.xml', os.path.join('..', 'channel_configurations.xml')):
                infos = load_channel_file(cand)
                if infos:
                    channels_provider = lambda i=infos: i
                    break
        self.setup = TestSetupPanel(channels_provider, positions_provider)
        self._build_ui()
        self._build_menus()
        self.setup.changed.connect(self._setup_changed)
        self.help.insert_requested.connect(self.editor.insert_snippet)
        self.help.open_example_requested.connect(self.open_example)
        self.editor.helpRequested.connect(self.help_on_word)
        self.editor.cursorLineChanged.connect(lambda l, c: self.label_pos.setText(f'Line {l}, column {c}'))
        self.editor.document().modificationChanged.connect(self._update_title)
        self.editor.textChanged.connect(self._text_changed)
        self._lint_timer = QTimer(self); self._lint_timer.setSingleShot(True); self._lint_timer.setInterval(450)
        self._lint_timer.timeout.connect(self.run_lint)
        self._auto_timer = QTimer(self); self._auto_timer.setSingleShot(True); self._auto_timer.setInterval(1500)
        self._auto_timer.timeout.connect(self._auto_test)
        self._poll = QTimer(self); self._poll.setInterval(60); self._poll.timeout.connect(self._poll_test)
        self._setup_changed()
        if job_path and os.path.isfile(job_path):
            self.load_file(job_path)
        else:
            self.new_job()
        self._restore_layout()
        self.run_lint()

    # --- construction ------------------------------------------------------------------------
    @staticmethod
    def _find_example_dir():
        for cand in ('job_examples', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'job_examples')):
            if os.path.isdir(cand):
                return cand
        return None

    def _setting(self, key, default=None):
        try:
            return self.settings.value(key, default) if self.settings else default
        except Exception:
            return default

    def _put_setting(self, key, value):
        try:
            if self.settings:
                self.settings.setValue(key, value)
        except Exception:
            pass

    def _build_ui(self):
        # editor + find bar
        self.editor = CodeEditor()
        self.find = FindBar(self.editor)
        left_top = QWidget()
        lt = QVBoxLayout(left_top); lt.setContentsMargins(0, 0, 0, 0); lt.setSpacing(0)
        lt.addWidget(self.editor, 1); lt.addWidget(self.find)
        # bottom tabs
        self.output = QTextBrowser()
        self.output.setOpenLinks(False)
        self.output.anchorClicked.connect(self._output_link)
        self.problems = QListWidget()
        self.problems.itemActivated.connect(self._problem_activated)
        self.problems.itemClicked.connect(self._problem_activated)
        self.events_table = QTableWidget(0, 7)
        self.events_table.setHorizontalHeaderLabels(['Time', 'Duration', 'Time point', 'Position', 'What', 'Channel', 'Line'])
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.events_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.events_table.horizontalHeader().setStretchLastSection(True)
        self.events_table.cellDoubleClicked.connect(self._event_row_activated)
        self.events_tp = QComboBox()
        self.events_tp.currentIndexChanged.connect(self._fill_events)
        ev_box = QWidget(); el = QVBoxLayout(ev_box); el.setContentsMargins(0, 0, 0, 0)
        er = QHBoxLayout(); er.addWidget(QLabel('Show time point')); er.addWidget(self.events_tp); er.addStretch(1)
        er.addWidget(QLabel('double click: go to the line in the code'))
        el.addLayout(er); el.addWidget(self.events_table, 1)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self.output, 'Output')
        self.bottom_tabs.addTab(self.problems, 'Problems')
        self.bottom_tabs.addTab(ev_box, 'Step list')
        self.bottom_tabs.addTab(self.setup, 'Test setup')
        self.left_split = QSplitter(Qt.Vertical)
        self.left_split.addWidget(left_top)
        self.left_split.addWidget(self.bottom_tabs)
        self.left_split.setSizes([520, 260])
        # plan panel
        self.plan_status = QLabel('Press Test (F5) to see what the job would do.')
        self.plan_status.setWordWrap(True)
        self.plan_status.setMargin(4)
        self.plan = PlanView()
        self.stage_map, self.budget, self.matrix = StageMapView(), BudgetView(), MatrixView()
        self.plan_scroll = QScrollArea()
        self.plan_scroll.setWidgetResizable(True)
        self.plan_scroll.setWidget(self.plan)
        self.plan_scroll.setFrameShape(QScrollArea.NoFrame)
        small = QSplitter(Qt.Horizontal)
        for v in (self.stage_map, self.budget, self.matrix):
            small.addWidget(v)
        self.plan_split = QSplitter(Qt.Vertical)
        self.plan_split.addWidget(self.plan_scroll)
        self.plan_split.addWidget(small)
        self.plan_split.setSizes([430, 220])
        bar = QHBoxLayout()
        self.btn_whole = QPushButton('Whole job'); self.btn_first = QPushButton('First time point')
        self.spin_tp = QSpinBox(); self.spin_tp.setRange(0, 0); self.btn_goto_tp = QPushButton('Show time point')
        hint = QLabel('mouse wheel: zoom | drag: move | double click: whole job | click an image: go to its line')
        hint.setStyleSheet('color: gray')
        for w in (self.btn_whole, self.btn_first, self.spin_tp, self.btn_goto_tp):
            bar.addWidget(w)
        bar.addStretch(1); bar.addWidget(hint)
        plan_widget = QWidget()
        pl = QVBoxLayout(plan_widget); pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.plan_status); pl.addLayout(bar); pl.addWidget(self.plan_split, 1)
        self.btn_whole.clicked.connect(self.plan.fit)
        self.btn_first.clicked.connect(lambda: self.plan.zoom_to_timepoint(0))
        self.btn_goto_tp.clicked.connect(lambda: self.plan.zoom_to_timepoint(self.spin_tp.value()))
        self.plan.eventClicked.connect(lambda line: self.editor.goto_line(line))
        self.plan.pointHovered.connect(self.stage_map.highlight_point)
        self.stage_map.pointHovered.connect(self.plan.highlight_point)
        # central
        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.addWidget(self.left_split)
        self.main_split.addWidget(plan_widget)
        self.main_split.setSizes([640, 640])
        self.setCentralWidget(self.main_split)
        # help dock
        self.help = HelpPanel(self.example_dir)
        self.help_dock = QDockWidget('Help', self)
        self.help_dock.setObjectName('help_dock')
        self.help_dock.setWidget(self.help)
        self.help_dock.setMinimumWidth(360)
        self.addDockWidget(Qt.RightDockWidgetArea, self.help_dock)
        self.resizeDocks([self.help_dock], [470], Qt.Horizontal)
        # status bar
        self.label_lint = QLabel('')
        self.label_pos = QLabel('')
        self.label_test = QLabel('')
        sb = self.statusBar()
        sb.addPermanentWidget(self.label_test)
        sb.addPermanentWidget(self.label_lint)
        sb.addPermanentWidget(self.label_pos)
        # toolbar
        tb = QToolBar('Main')
        tb.setObjectName('main_toolbar')
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.toolbar = tb
        self.resize(1600, 900)
        scr = QApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.resize(min(1600, int(g.width() * 0.96)), min(950, int(g.height() * 0.92)))

    def _act(self, text, slot, shortcut=None, tip=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu('&File')
        self.act_new = self._act('New job', self.new_job, 'Ctrl+N', 'Start a new job from the template')
        self.act_open = self._act('Open...', self.open_dialog, 'Ctrl+O')
        self.act_save = self._act('Save', self.save, 'Ctrl+S')
        self.act_save_as = self._act('Save as...', self.save_as, 'Ctrl+Shift+S')
        self.act_use = self._act('Use in JOBs tab', self.use_in_jobs_tab, 'Ctrl+U', 'Save the job and select it in the JOBs tab of the main program (it is started there)')
        self.act_use.setEnabled(self.use_job_callback is not None)
        self.menu_examples = f.addMenu('New from example')
        self._fill_example_menu()
        f.addAction(self.act_new); f.addAction(self.act_open); f.addMenu(self.menu_examples); f.addSeparator()
        f.addAction(self.act_save); f.addAction(self.act_save_as); f.addSeparator(); f.addAction(self.act_use); f.addSeparator()
        f.addAction(self._act('Close', self.close, 'Ctrl+W'))
        e = mb.addMenu('&Edit')
        e.addAction(self._act('Undo', self.editor.undo, 'Ctrl+Z')); e.addAction(self._act('Redo', self.editor.redo, 'Ctrl+Y')); e.addSeparator()
        e.addAction(self._act('Find...', self.find.open_bar, 'Ctrl+F'))
        e.addAction(self._act('Find next', self.find.find_next, 'F3'))
        e.addAction(self._act('Comment / uncomment lines', self.editor.toggle_comment, 'Ctrl+/'))
        e.addAction(self._act('Completion list', lambda: self.editor.show_completion(manual=True), 'Ctrl+Space'))
        sn = e.addMenu('Insert snippet')
        for title, desc, code in apidoc.SNIPPETS:
            a = self._act(title, lambda _=False, c=code: self.editor.insert_snippet(c), tip=desc)
            sn.addAction(a)
        r = mb.addMenu('&Test')
        self.act_test = self._act('Test job (dry run)', self.run_test, 'F5', 'Run the job against a simulated microscope: no stage movement, no light, simulated time')
        self.act_cancel = self._act('Cancel test', self.cancel_test, 'Ctrl+.')
        self.act_cancel.setEnabled(False)
        self.act_auto = self._act('Test automatically while typing', self._auto_toggled, tip='Run the test 1.5 s after the last change', checkable=True)
        self.act_auto.setChecked(str(self._setting('auto_test', 'false')).lower() == 'true')
        r.addAction(self.act_test); r.addAction(self.act_cancel); r.addSeparator(); r.addAction(self.act_auto)
        v = mb.addMenu('&View')
        v.addAction(self.help_dock.toggleViewAction())
        v.addAction(self._act('Show test setup', lambda: self.bottom_tabs.setCurrentWidget(self.setup)))
        v.addAction(self._act('Whole job in the plan', self.plan.fit))
        h = mb.addMenu('&Help')
        h.addAction(self._act('Help on the word at the cursor', lambda: self.help_on_word(self.editor.word_at_cursor()[0]), 'F1'))
        h.addAction(self._act('Quick start', lambda: self._show_topic('Quick start')))
        h.addAction(self._act('Python in 2 minutes', lambda: self._show_topic('Python in 2 minutes')))
        h.addAction(self._act('All functions', lambda: self._show_help_tab(0)))
        # toolbar
        tb = self.toolbar
        style = self.style()
        self.act_test.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.act_save.setIcon(style.standardIcon(QStyle.SP_DialogSaveButton))
        self.act_open.setIcon(style.standardIcon(QStyle.SP_DialogOpenButton))
        self.act_new.setIcon(style.standardIcon(QStyle.SP_FileIcon))
        for a in (self.act_new, self.act_open, self.act_save):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_test)
        tb.addAction(self.act_cancel)
        tb.addSeparator()
        tb.addAction(self.act_use)
        tb.addSeparator()
        help_act = self.help_dock.toggleViewAction()
        help_act.setText('Help panel')
        tb.addAction(help_act)

    def _fill_example_menu(self):
        self.menu_examples.clear()
        d = self.example_dir
        if not d or not os.path.isdir(d):
            a = self.menu_examples.addAction('(no examples found)')
            a.setEnabled(False)
            return
        for fname in sorted(os.listdir(d)):
            if fname.endswith('.py') and not fname.startswith('_'):
                path = os.path.join(d, fname)
                t = source_title(path)
                a = self._act(fname + (f'  -  {t[:60]}' if t else ''), lambda _=False, p=path: self.open_example(p))
                self.menu_examples.addAction(a)

    def _restore_layout(self):
        try:
            if self.settings:
                geo, st = self.settings.value('geometry'), self.settings.value('state')
                if geo is not None:
                    self.restoreGeometry(geo)
                if st is not None:
                    self.restoreState(st)
        except Exception:
            pass

    # --- document handling -------------------------------------------------------------------
    def _display_name(self):
        return os.path.basename(self.path) if self.path else 'untitled job'

    def _update_title(self, *_):
        mod = '*' if self.editor.document().isModified() else ''
        self.setWindowTitle(f'{self._display_name()}{mod} - Job designer')

    def _maybe_save(self):
        """True if the editor may be replaced/closed"""
        if not self.editor.document().isModified():
            return True
        box = QMessageBox.question(self, 'Job designer', f'Save the changes to {self._display_name()}?',
                                   QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if box == QMessageBox.Cancel:
            return False
        if box == QMessageBox.Save:
            return self.save()
        return True

    def _set_text(self, text, path=None, modified=False):
        self.editor.setPlainText(text)
        self.path = path
        self.editor.document().setModified(modified)
        self.editor.moveCursor(QTextCursor.Start)
        self.editor.set_error_line(None)
        self._clear_results()
        self._update_title()
        self.run_lint()

    def new_job(self):
        if not self._maybe_save():
            return
        self._set_text(apidoc.NEW_JOB_TEMPLATE, None, modified=False)

    def open_dialog(self):
        if not self._maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Open job', self.last_dir, 'Python files (*.py);;All files (*)')
        if path:
            self.load_file(path)

    def load_file(self, path, check_save=False):
        if check_save and not self._maybe_save():
            return False
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError as e:
            QMessageBox.warning(self, 'Open job', f'Cannot read {path}:\n{e}')
            return False
        self._set_text(text, os.path.abspath(path), modified=False)
        self.last_dir = os.path.dirname(os.path.abspath(path))
        self._put_setting('last_dir', self.last_dir)
        self.statusBar().showMessage(f'Opened {path}', 4000)
        return True

    def open_example(self, path):
        """a copy of an example in the editor (untitled)"""
        if not self._maybe_save():
            return
        try:
            text = open(path, encoding='utf-8').read()
        except OSError as e:
            QMessageBox.warning(self, 'Example', str(e))
            return
        self._set_text(text, None, modified=True)
        self._suggested_name = os.path.basename(path)
        self.statusBar().showMessage('Copy of ' + os.path.basename(path) + ': save it under a new name.', 6000)

    def save(self):
        if not self.path:
            return self.save_as()
        return self._write(self.path)

    def save_as(self):
        start = os.path.join(self.last_dir or '', getattr(self, '_suggested_name', 'my_job.py') if not self.path else os.path.basename(self.path))
        path, _ = QFileDialog.getSaveFileName(self, 'Save job as', start, 'Python files (*.py)')
        if not path:
            return False
        if not path.lower().endswith('.py'):
            path += '.py'
        return self._write(path)

    def _write(self, path):
        try:
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(self.editor.toPlainText())
        except OSError as e:
            QMessageBox.warning(self, 'Save job', f'Cannot write {path}:\n{e}')
            return False
        self.path = os.path.abspath(path)
        self.last_dir = os.path.dirname(self.path)
        self._put_setting('last_dir', self.last_dir)
        self.editor.document().setModified(False)
        self._update_title()
        self.statusBar().showMessage(f'Saved {self.path}', 4000)
        return True

    def use_in_jobs_tab(self):
        if self.use_job_callback is None:
            return
        if not self.path or self.editor.document().isModified():
            if not self.save():
                return
        msg = self.use_job_callback(self.path)
        self.statusBar().showMessage(msg or f'{os.path.basename(self.path)} is selected in the JOBs tab.', 8000)

    def closeEvent(self, ev):
        if not self._maybe_save():
            ev.ignore()
            return
        self._closing = True
        if self.test_run is not None:
            self.test_run.cancel()
        self._poll.stop()
        self._put_setting('geometry', self.saveGeometry())
        self._put_setting('state', self.saveState())
        self._put_setting('auto_test', self.act_auto.isChecked())
        ev.accept()

    # --- live checking -----------------------------------------------------------------------
    def _text_changed(self):
        self._lint_timer.start()
        if self.act_auto.isChecked():
            self._auto_timer.start()

    def _setup_changed(self):
        names = list(self.setup.channel_infos())
        self.editor.channel_names = names
        self.editor.channel_infos = self.setup.channel_infos()
        self.editor.group_names = self.setup.group_names()
        self._lint_timer.start()
        if self.act_auto.isChecked() and hasattr(self, '_auto_timer'):
            self._auto_timer.start()

    def run_lint(self):
        self._lint_timer.stop()
        issues = lint(self.editor.toPlainText(), self.setup.channel_infos().keys() or None, self.setup.group_names() or None)
        self.issues = issues
        self.editor.set_issues(issues)
        self.problems.clear()
        icons = {'error': '✖', 'warning': '⚠', 'info': 'ℹ'}
        for i in issues:
            it = QListWidgetItem(f'{icons[i.severity]}  line {i.line}: {i.message}')
            it.setData(Qt.UserRole, i.line)
            it.setForeground(QColor({'error': '#e03131', 'warning': '#e67700', 'info': '#1c7ed6'}[i.severity]))
            self.problems.addItem(it)
        if self.last_result is not None and self.last_result.status not in ('completed',):
            msg = (self.last_result.error or self.last_result.status).splitlines()[0]
            it = QListWidgetItem(f'✖  test: {msg}')
            it.setData(Qt.UserRole, self.last_result.error_line or 0)
            it.setForeground(QColor('#e03131'))
            self.problems.insertItem(0, it)
        e, w = lint_summary(issues)
        self.bottom_tabs.setTabText(1, f'Problems ({e + w})' if (e + w) else 'Problems')
        if e:
            self.label_lint.setText(f'✖ {e} error(s), {w} warning(s)')
            self.label_lint.setStyleSheet('color: #e03131')
        elif w:
            self.label_lint.setText(f'⚠ {w} warning(s)')
            self.label_lint.setStyleSheet('color: #e67700')
        else:
            self.label_lint.setText('✔ no problems found')
            self.label_lint.setStyleSheet('color: #2b8a3e')

    def _problem_activated(self, item):
        line = item.data(Qt.UserRole)
        if line:
            self.editor.goto_line(int(line))

    # --- test --------------------------------------------------------------------------------
    def _auto_toggled(self):
        self._put_setting('auto_test', self.act_auto.isChecked())

    def _auto_test(self):
        if self.test_run is None and not any(i.severity == 'error' for i in getattr(self, 'issues', [])):
            self.run_test(quiet=True)

    def _clear_results(self):
        self.last_result = None
        self.plan.clear('Press Test (F5) to see what the job would do.')
        for v in (self.stage_map, self.budget, self.matrix):
            v.clear('')
        self.plan_status.setText('Press Test (F5) to see what the job would do.')
        self.plan_status.setStyleSheet('')
        self.output.clear()
        self.events_table.setRowCount(0)
        self.events_tp.clear()
        self.label_test.setText('')

    def run_test(self, quiet=False):
        if self.test_run is not None:
            return
        try:
            points = self.setup.points()
        except ValueError as e:
            self.bottom_tabs.setCurrentWidget(self.setup)
            self.setup.tabs.setCurrentIndex(0)
            self.statusBar().showMessage('Cannot test: ' + str(e), 8000)
            if not quiet:
                QMessageBox.warning(self, 'Test positions', str(e))
            return
        infos = self.setup.channel_infos()
        if not infos:
            self.statusBar().showMessage('Cannot test: no channels in the Test setup.', 8000)
            return
        tmpdir = tempfile.mkdtemp(prefix='squid_job_')
        name = os.path.basename(self.path) if self.path else 'job.py'
        path = os.path.join(tmpdir, name)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(self.editor.toPlainText())
        self._tmpdir = tmpdir
        self.last_points = points
        self.test_run = TestRun(path, points, infos, self.setup.timing())
        self.test_run.start()
        self.act_test.setEnabled(False)
        self.act_cancel.setEnabled(True)
        self.label_test.setText('Testing ...')
        self._poll.start()

    def cancel_test(self):
        if self.test_run is not None:
            self.test_run.cancel()
            self.label_test.setText('Cancelling ...')

    def _poll_test(self):
        run = self.test_run
        if run is None:
            self._poll.stop()
            return
        age = time.time() - run.started
        if not run.done and age > TEST_TIMEOUT_S and not getattr(run, '_killed', False):
            run.cancel()
            run.kill()
            run._killed = True
            self.label_test.setText('The job does not finish: stopping it ...')
        if run.done:
            self._poll.stop()
            self.test_run = None
            self.act_test.setEnabled(True)
            self.act_cancel.setEnabled(False)
            shutil.rmtree(getattr(self, '_tmpdir', ''), ignore_errors=True)
            if not self._closing:
                self._show_result(run, timed_out=getattr(run, '_killed', False))

    def _show_result(self, run, timed_out=False):
        res = run.result
        if res is None:
            self.output.setHtml(f'<h3 style="color:#c0392b">The test could not run</h3><pre>{html.escape(run.crash or "unknown error")}</pre>')
            self.label_test.setText('Test failed to run')
            return
        self.last_result = res
        ok = res.status == 'completed'
        s = res.stats or {}
        pts = self.last_points
        channels = list(self.setup.channel_infos())
        # plan
        self.plan.set_result(res, pts, channels)
        for v in (self.stage_map, self.budget, self.matrix):
            v.set_result(res, pts, channels)
        n_tp = s.get('timepoints', 0)
        self.spin_tp.setRange(0, max(0, n_tp - 1))
        # status line
        if ok and getattr(res, 'incomplete', False):
            head = (f'⚠ Test stopped at its operation limit: only the first {fmt_dur(res.duration_s)} of the job ({n_tp} time points, '
                    f'{s.get("images", 0)} images) could be tested. The job can still be started.')
            self.plan_status.setStyleSheet('color: #e67700; font-weight: bold')
        elif ok:
            head = (f'✔ Test passed: {s.get("images", 0)} images, {s.get("autofocus", 0)} autofocus runs, {n_tp} time points, '
                    f'estimated duration {fmt_dur(res.duration_s)}')
            self.plan_status.setStyleSheet('color: #2b8a3e; font-weight: bold')
        else:
            first = (res.error or res.status).splitlines()[0]
            head = f'✖ Test {res.status}: {first}' + (f' (line {res.error_line})' if res.error_line else '')
            self.plan_status.setStyleSheet('color: #c0392b; font-weight: bold')
        if timed_out:
            head = ('\u2716 The test was stopped after 30 s: the job does not end. Is there a loop without any scope call '
                    '(e.g. "while True: pass")? Loops should contain scope.wait(...) or other scope calls.')
            self.plan_status.setStyleSheet('color: #c0392b; font-weight: bold')
        if s.get('late_timepoints'):
            head += f'  ⚠ {s["late_timepoints"]} time point(s) start late (max {s["max_late_s"]:.0f} s): the interval is shorter than one pass'
        self.plan_status.setText(head)
        self.label_test.setText(('Test passed' if ok else f'Test {res.status}') + f' - {fmt_dur(res.duration_s)} estimated')
        self.editor.set_error_line(res.error_line if not ok else None)
        if not ok and res.error_line:
            self.editor.goto_line(res.error_line, flash=False)
        # output
        self.output.setHtml(self._output_html(run, res, ok))
        self.bottom_tabs.setCurrentIndex(0)
        # step list
        self._fill_events_filter(res)
        self.run_lint()

    def _output_html(self, run, res, ok):
        h = [apidoc.CSS]
        if ok and getattr(res, 'incomplete', False):
            h.append('<h3 style="color:#e67700">⚠ Test passed only partly: the job is longer than the test allows</h3>')
        elif ok:
            h.append('<h3 style="color:#2b8a3e">✔ Test passed</h3>')
        else:
            h.append(f'<h3 style="color:#c0392b">✖ Test {html.escape(res.status)}</h3>')
            link = f' <a href="line:{res.error_line}">go to line {res.error_line}</a>' if res.error_line else ''
            h.append(f'<pre style="color:#c0392b;white-space:pre-wrap">{html.escape(res.error)}</pre>{link}')
        h.append('<p style="color:gray">Dry run: simulated time and a simulated microscope (nothing moves). '
                 'Durations are estimates from the timing in the Test setup; images are dummy zeros.</p>')
        text = describe(res)
        lines = [l for l in text.splitlines() if not l.startswith('error:') and not l.startswith('warning:') and not l.startswith('status:')]
        if lines:
            h.append('<pre>' + html.escape('\n'.join(lines)) + '</pre>')
        warns = list(res.warnings)
        if warns:
            h.append('<h4>Warnings</h4><ul>' + ''.join(f'<li style="color:#e67700">{html.escape(w)}</li>' for w in warns) + '</ul>')
        out = run.stdout.getvalue()
        if out.strip():
            h.append('<h4>Printed by the job (print)</h4><pre>' + html.escape(out[-6000:]) + '</pre>')
        msgs = [m for m in run.messages]
        if msgs:
            shown = msgs[:200]
            more = f'\n... {len(msgs) - len(shown)} more lines' if len(msgs) > len(shown) else ''
            h.append('<h4>Job log (scope.log and time points, simulated time)</h4><pre>' + html.escape('\n'.join(shown) + more) + '</pre>')
        return ''.join(h)

    def _output_link(self, url):
        s = url.toString()
        if s.startswith('line:'):
            try:
                self.editor.goto_line(int(s.split(':')[1]))
            except ValueError:
                pass

    # --- step list ---------------------------------------------------------------------------
    def _fill_events_filter(self, res):
        self.events_tp.blockSignals(True)
        self.events_tp.clear()
        self.events_tp.addItem('all (first 3000 steps)', None)
        tps = sorted({e['tp'] for e in res.events if e.get('tp') is not None})
        for t in tps[:500]:
            self.events_tp.addItem(str(t), t)
        self.events_tp.blockSignals(False)
        self._fill_events()

    def _fill_events(self, *_):
        res = self.last_result
        tbl = self.events_table
        tbl.setRowCount(0)
        if res is None:
            return
        sel = self.events_tp.currentData()
        pts = {p.index: p for p in self.last_points}
        rows = []
        for e in res.events:
            if e['kind'] == 'timepoint':
                rows.append(e)
            elif sel is None or e.get('tp') == sel:
                rows.append(e)
            if sel is None and len(rows) >= 3000:
                break
        if sel is not None:
            rows = [e for e in rows if e.get('tp') == sel]
        tbl.setRowCount(len(rows))
        for r, e in enumerate(rows):
            p = pts.get(e.get('point'))
            what = {'timepoint': 'time point starts', 'wait': 'wait', 'move': 'move', 'capture': 'image', 'autofocus': 'autofocus'}.get(e['kind'], e['kind'])
            vals = [fmt_t(e['t0']), fmt_dur(e['t1'] - e['t0']) if e['kind'] != 'timepoint' else ('late %.0f s' % e['late'] if e.get('late', 0) > 0.5 else ''),
                    '' if e.get('tp') is None else str(e['tp']), (f'#{p.index} {p.id}' if p else ''), what, e.get('channel') or '', str(e.get('line') or '')]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if e['kind'] == 'timepoint':
                    f = it.font(); f.setBold(True); it.setFont(f)
                tbl.setItem(r, c, it)
        tbl.resizeColumnsToContents()

    def _event_row_activated(self, row, col):
        it = self.events_table.item(row, 6)
        if it and it.text().isdigit():
            self.editor.goto_line(int(it.text()))

    # --- help --------------------------------------------------------------------------------
    def _show_help_tab(self, index):
        self.help_dock.show()
        self.help.setCurrentIndex(index)

    def _show_topic(self, title):
        self.help_dock.show()
        self.help.show_topic(title)

    def help_on_word(self, word):
        self.help_dock.show()
        if word and self.help.show_function(word):
            return
        if word in ('Point', 'TimePoint') and self.help.show_function(word):
            return
        self.help.setCurrentIndex(0)
        self.help.search.setFocus()
        self.statusBar().showMessage('Put the cursor on a function name (e.g. scope.capture) and press F1, or search in the Help panel.', 6000)


# ------------------------------------------------------------------------------------------------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(sys.argv)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.abspath(argv[0]) if argv else None
    os.chdir(here)             # the software folder (examples, channel_configurations.xml)
    win = JobDesignerWindow(job_path=path)
    win.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
