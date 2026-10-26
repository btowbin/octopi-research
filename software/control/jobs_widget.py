"""JOBs tab: select a job file (Python, see control/jobs/README.md), check it (dry run), run it.

The positions (and their IDs/groups) come from the Flexible Multipoint tab. Starting a job uses the same
experiment preparation (saving folder, configurations) and the same MultiPointController/Worker thread
as a standard acquisition; the worker runs the job instead of the standard loops.
"""
import os
import sys
import traceback
import json
import shutil

from qtpy.QtCore import Signal, Qt
from qtpy.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QFileDialog,
                            QMessageBox, QPlainTextEdit, QDialog, QDoubleSpinBox, QComboBox)

from control.jobs import make_points, dry_run, describe, job_documentation, JobError
from control.jobs_live import LiveJobView
from control.resizable_panel import make_combo_compact


class PlanDialog(QDialog):
    """Shows the graphical plan of a job (control/jobs/plot.py); the detail panel can be set to any time range."""

    def __init__(self, result, points, channels, parent=None):
        super().__init__(parent)
        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        except ImportError:        # matplotlib < 3.5
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        self.setWindowTitle('Job plan (dry run, estimated times)')
        self.resize(1250, 900)
        self.result, self.points, self.channels = result, points, channels
        self._canvas_class = FigureCanvasQTAgg
        self.canvas = None
        self.spin_from, self.spin_to = QDoubleSpinBox(), QDoubleSpinBox()
        for sp in (self.spin_from, self.spin_to):
            sp.setRange(0, 1e6); sp.setDecimals(1)
        self.spin_to.setValue(0)
        btn_redraw = QPushButton('Redraw detail'); btn_save = QPushButton('Save image...')
        row = QHBoxLayout()
        row.addWidget(QLabel('Detail panel from (min)')); row.addWidget(self.spin_from)
        row.addWidget(QLabel('to (min; 0 = first time point)')); row.addWidget(self.spin_to)
        row.addWidget(btn_redraw); row.addStretch(1); row.addWidget(btn_save)
        self.layout_ = QVBoxLayout(self); self.layout_.addLayout(row)
        btn_redraw.clicked.connect(self.draw); btn_save.clicked.connect(self.save)
        self.draw()

    def draw(self):
        from control.jobs.plot import plot_job
        t0, t1 = self.spin_from.value() * 60.0, self.spin_to.value() * 60.0
        fig = plot_job(self.result, self.points, channels=self.channels, detail_range=(t0, t1) if t1 > t0 else None)
        canvas = self._canvas_class(fig)          # a new canvas for every figure (works with all matplotlib versions)
        if self.canvas is not None:
            self.layout_.removeWidget(self.canvas)
            self.canvas.setParent(None)
            self.canvas.deleteLater()
        self.canvas = canvas
        self.layout_.addWidget(canvas, 1)
        canvas.draw()

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save job plan', 'job_plan.png', 'Images (*.png *.pdf *.svg)')
        if path:
            self.canvas.figure.savefig(path, bbox_inches='tight')

CACHE_FILE = os.path.join('cache', 'jobs_settings.json')
EXAMPLE_DIR = 'job_examples'
UNSUPPORTED_CHANNELS = ('RGB', 'USB Spectrometer')


class JobsWidget(QFrame):

    signal_acquisition_started = Signal(bool)     # same meaning as in the other acquisition tabs (locks the stage controls)
    signal_job_paused = Signal(bool)              # True: job paused (stage controls and Flexible Multipoint tab usable); False: resumed

    def __init__(self, multipointController, configurationManager, positionsWidget, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mpc = multipointController
        self.configurationManager = configurationManager
        self.positionsWidget = positionsWidget      # Flexible Multipoint tab: location_list, location_ids, saving dir, experiment ID
        self.running = False
        self.paused = False
        self._n_points_at_start = 0
        self._build()
        self._load_cache()
        self.mpc.signal_job_message.connect(self._on_job_message)
        self.mpc.signal_job_status.connect(self._on_live_message)
        self.mpc.signal_job_status.connect(self._on_job_status)
        self.mpc.acquisitionFinished.connect(self.acquisition_is_finished)

    # --- layout
    def _build(self):
        self.lineEdit_job = QLineEdit()
        self.lineEdit_job.setPlaceholderText('job file (.py) defining run_job(scope)')
        self.btn_select_job = QPushButton('Select job...')
        self.btn_example = QPushButton('New from example...')
        self.btn_example.setToolTip('Copy an example job to a place of your choice and select it')
        self.lineEdit_savingDir = QLineEdit()
        self.btn_savingDir = QPushButton('Saving dir...')
        self.lineEdit_experimentID = QLineEdit('job')
        self.label_points = QLabel('')
        self.label_points.setWordWrap(True)   # long text must not widen the control panel
        self.btn_designer = QPushButton('Job designer...')
        self.btn_designer.setToolTip('Opens the Job designer: an editor to write a job, test it (simulated, nothing moves), see a graphic of what it does, '
                                     'and get help on all functions. Does not influence a running job.')
        self.btn_check = QPushButton('Check job (dry run)')
        self.btn_check.setToolTip('Runs the job with simulated time and no hardware: finds errors, counts images and estimates the duration')
        self.btn_plan = QPushButton('Show job plan (graph)')
        self.btn_plan.setToolTip('Draws what the job would do and when (timeline per position, stage map, time per time point), from a dry run')
        self.btn_start = QPushButton('Start job')
        self.btn_start.setCheckable(True)
        self.btn_start.setStyleSheet('background-color: #C2C2FF')
        self.btn_pause = QPushButton('Pause job')
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip('Pause at the next safe point (between two operations). While paused you can move the stage and edit the '
                                  'positions in the Flexible Multipoint tab; "Resume job" continues where the job was (waiting times continue, '
                                  'time spent paused is not counted). Positions cannot be added or removed while paused.')
        self.combo_save_as = QComboBox()
        for value, text in (('multipage', 'multipage TIFF, written directly'), ('merge', 'single files, merged into multipage TIFF later'), ('single', 'single files')):
            self.combo_save_as.addItem(text, value)
        self.combo_save_as.setToolTip('multipage: one file per point and time point, Time0003_Point0002.tiff (all channels; z-stacks and movies as ..._Zstack.tiff / ..._Movie.tiff), '
                                      'written directly from memory.\n'
                                      'single files, merged later: every image is first written as its own file; when the job waits (or at the end) they are merged into the '
                                      'multipage file and deleted. Slower, less memory; safer for very large images.\n'
                                      'single files: one TIFF per image in a folder per time point, <ID>_0_0_<z>_<channel>.tiff (as the Flexible Multipoint tab).\n'
                                      'A job can override this per call with save_as="multipage" / "merge" / "single".')
        self.label_status = QLabel('')
        self.label_status.setWordWrap(True)
        self.live = LiveJobView()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)

        row1 = QHBoxLayout(); row1.addWidget(QLabel('Job file')); row1.addWidget(self.lineEdit_job, 1)
        row1.addWidget(self.btn_select_job); row1.addWidget(self.btn_example); row1.addWidget(self.btn_designer)
        self.lineEdit_savingDir.setMinimumWidth(250)
        self.lineEdit_savingDir.setToolTip('Saving directory (full path)')
        self.lineEdit_savingDir.textChanged.connect(lambda t, w=self.lineEdit_savingDir: w.setToolTip(t or 'Saving directory (full path)'))
        self.lineEdit_experimentID.setMinimumWidth(160)
        make_combo_compact(self.combo_save_as, 12, 420)   # long item texts must not widen the control panel
        row2 = QHBoxLayout(); row2.addWidget(QLabel('Saving dir')); row2.addWidget(self.lineEdit_savingDir, 1); row2.addWidget(self.btn_savingDir)
        row2b = QHBoxLayout(); row2b.addWidget(QLabel('Experiment ID')); row2b.addWidget(self.lineEdit_experimentID, 1)
        row2b.addSpacing(10); row2b.addWidget(QLabel('Save as')); row2b.addWidget(self.combo_save_as, 1)
        row3 = QHBoxLayout(); row3.addWidget(self.btn_check); row3.addWidget(self.btn_plan); row3.addWidget(self.btn_start); row3.addWidget(self.btn_pause)
        layout = QVBoxLayout()
        layout.addLayout(row1); layout.addLayout(row2); layout.addLayout(row2b); layout.addWidget(self.label_points)
        layout.addLayout(row3); layout.addWidget(self.label_status); layout.addWidget(self.live); layout.addWidget(self.log, 1)
        self.setLayout(layout)

        self.btn_select_job.clicked.connect(self.select_job)
        self.lineEdit_job.editingFinished.connect(self._job_path_edited)
        self.btn_example.clicked.connect(self.new_from_example)
        self.btn_designer.clicked.connect(self.open_designer)
        self.btn_savingDir.clicked.connect(self.select_saving_dir)
        self.btn_check.clicked.connect(self.check_job)
        self.btn_plan.clicked.connect(self.show_plan)
        self.btn_start.clicked.connect(self.toggle_job)
        self.btn_pause.clicked.connect(self.toggle_pause)

    def showEvent(self, event):
        super().showEvent(event)
        # take over saving folder / experiment ID typed in the Flexible Multipoint tab if still empty
        pw = self.positionsWidget
        if not self.lineEdit_savingDir.text() and pw.lineEdit_savingDir.text():
            self.lineEdit_savingDir.setText(pw.lineEdit_savingDir.text())
        self.refresh_points_label()

    # --- persistence of the last job / folder
    def _load_cache(self):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            self.lineEdit_job.setText(d.get('job_file', ''))
            self.lineEdit_savingDir.setText(d.get('saving_dir', ''))
            k = self.combo_save_as.findData(d.get('save_as', 'multipage'))
            self.combo_save_as.setCurrentIndex(k if k >= 0 else 0)
        except Exception:
            pass

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(dict(job_file=self.lineEdit_job.text(), saving_dir=self.lineEdit_savingDir.text(), save_as=self.combo_save_as.currentData()), f)
        except Exception:
            pass

    # --- messages of the running job; runs started by the Single measurement tab (mpc.job_owner == 'single') are not shown here
    def _mine(self):
        return getattr(self.mpc, 'job_owner', 'jobs') != 'single'

    def _on_job_message(self, text):
        if self._mine():
            self.append_log(text)

    def _on_live_message(self, msg):
        if self._mine():
            self.live.on_message(msg)

    # --- helpers
    def append_log(self, text):
        self.log.appendPlainText(text)

    def _points(self):
        pw = self.positionsWidget
        ids = [str(i) for i in pw.location_ids]
        return make_points(ids, [tuple(r) for r in pw.location_list])

    def refresh_points_label(self):
        pts = self._points()
        groups = {}
        for p in pts:
            groups[p.group] = groups.get(p.group, 0) + 1
        if pts:
            self.label_points.setText(f'{len(pts)} positions from the Flexible Multipoint tab; groups: ' + ', '.join(f'{g} ({n})' for g, n in groups.items()))
        else:
            self.label_points.setText('No positions yet: define them in the Flexible Multipoint tab (the group is the part of the "group" column before the first "-").')

    def _channel_infos(self):
        return {c.name: dict(exposure_ms=c.exposure_time, gain=c.analog_gain, intensity=c.illumination_intensity)
                for c in self.configurationManager.configurations if not any(u in c.name for u in UNSUPPORTED_CHANNELS)}

    def _say(self, text):
        """Message in the status line AND the log (and returns None, for 'nothing was done')."""
        self.label_status.setText(text.splitlines()[0])
        self.log.appendPlainText(text)
        return None

    def _warn(self, title, text):
        QMessageBox.warning(self, title, text)

    # --- buttons
    def show_documentation(self):
        """Print what the job does and what the user has to provide (docstring of run_job) in the log and the terminal."""
        path = self.lineEdit_job.text().strip()
        if not path:
            return None
        self._doc_shown_for = path
        text = job_documentation(path)
        self.log.appendPlainText(text + '\n')
        print(text + '\n')
        return text

    def _job_path_edited(self):
        path = self.lineEdit_job.text().strip()
        if path and path != getattr(self, '_doc_shown_for', None) and os.path.isfile(path):
            self._save_cache()
            self.show_documentation()

    def select_job(self):
        start = os.path.dirname(self.lineEdit_job.text()) or EXAMPLE_DIR
        path, _ = QFileDialog.getOpenFileName(self, 'Select job file', start, 'Python files (*.py);;All files (*)')
        if path:
            self.lineEdit_job.setText(path)
            self._save_cache()
            self.show_documentation()
            self.check_job()

    def new_from_example(self):
        examples = sorted(f for f in os.listdir(EXAMPLE_DIR) if f.endswith('.py')) if os.path.isdir(EXAMPLE_DIR) else []
        if not examples:
            self._warn('Examples', 'No examples found in ' + EXAMPLE_DIR)
            return
        src, _ = QFileDialog.getOpenFileName(self, 'Choose an example', EXAMPLE_DIR, 'Python files (*.py)')
        if not src:
            return
        dst, _ = QFileDialog.getSaveFileName(self, 'Save a copy of the example as', os.path.basename(src), 'Python files (*.py)')
        if dst:
            shutil.copy2(src, dst)
            self.lineEdit_job.setText(dst)
            self._save_cache()
            self.append_log(f'copied {os.path.basename(src)} to {dst}; edit it with any text editor, then press "Check job".')
            self.show_documentation()

    # --- job designer
    def open_designer(self):
        """Open (or bring to the front) the Job designer window; it uses the positions and channels of this program for its tests."""
        try:
            win = getattr(self, 'designer', None)
            if win is not None:
                try:
                    win.isVisible()  # raises RuntimeError if the closed window was already deleted (WA_DeleteOnClose)
                except RuntimeError:
                    win = None
                    self.designer = None
            if win is None or not win.isVisible():
                from control.jobs_designer import JobDesignerWindow
                path = self.lineEdit_job.text().strip()
                win = JobDesignerWindow(channels_provider=self._channel_infos, positions_provider=self._positions_for_designer,
                                        job_path=path if os.path.isfile(path) else None, example_dir=EXAMPLE_DIR if os.path.isdir(EXAMPLE_DIR) else None,
                                        use_job_callback=self.use_job_from_designer)
                win.setAttribute(Qt.WA_DeleteOnClose, True)
                win.destroyed.connect(self._designer_destroyed)
                self.designer = win
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception:
            self._say('Could not open the Job designer:\n' + traceback.format_exc())

    def _designer_destroyed(self, *args):
        """The designer window was closed and deleted: forget it so that it can be opened again."""
        self.designer = None

    def _positions_for_designer(self):
        pw = self.positionsWidget
        return [str(i) for i in pw.location_ids], [tuple(r) for r in pw.location_list]

    def use_job_from_designer(self, path):
        """Called by the designer: select its (saved) job file here. Returns a message for the designer's status bar."""
        if self.running:
            return 'A job is running: it was not changed. Select the new job here after it has finished.'
        self.lineEdit_job.setText(path)
        self._save_cache()
        self._doc_shown_for = path
        self.show_documentation()
        self.check_job()
        return f'{os.path.basename(path)} is selected in the JOBs tab (checked). Press "Start job" there.'

    def select_saving_dir(self):
        d = QFileDialog.getExistingDirectory(self, 'Select folder')
        if d:
            self.lineEdit_savingDir.setText(d)
            self._save_cache()

    def check_job(self):
        """Dry run; returns the JobResult (None if no job file)."""
        self.refresh_points_label()
        path = self.lineEdit_job.text().strip()
        if not path:
            return self._say('Select a job file first (button "Select job..." or "New from example...").')
        pts = self._points()
        if not pts:
            return self._say('No positions: define them in the Flexible Multipoint tab first (the JOBs tab uses that position list).')
        try:
            res = dry_run(path, pts, self._channel_infos(), record=True)
            self._last_plan = (res, pts)
        except Exception:
            return self._say('Internal error in the job check:\n' + traceback.format_exc())
        self.log.appendPlainText('--- check of ' + os.path.basename(path) + ' ---\n' + describe(res))
        self.label_status.setText('Job OK (dry run)' if res.status == 'completed' else 'Job has a problem: ' + (res.error.splitlines()[0] if res.error else res.status))
        return res

    def show_plan(self):
        """Dry run with recording and a graphical display of the plan."""
        self.refresh_points_label()
        path = self.lineEdit_job.text().strip()
        pts = self._points()
        if not path:
            return self._say('Select a job file first (button "Select job..." or "New from example...").')
        if not pts:
            return self._say('No positions: define them in the Flexible Multipoint tab first (the JOBs tab uses that position list).')
        try:
            infos = self._channel_infos()
            res = dry_run(path, pts, infos, record=True)
        except Exception:
            return self._say('Internal error in the job plan:\n' + traceback.format_exc())
        self.log.appendPlainText('--- plan of ' + os.path.basename(path) + ' ---\n' + describe(res))
        self.label_status.setText('Job OK (dry run)' if res.status == 'completed' else 'Job has a problem: ' + (res.error.splitlines()[0] if res.error else res.status))
        if res.status != 'completed' and not res.events:
            self._say('The job could not be drawn: ' + (res.error or res.status))
        try:
            dlg = PlanDialog(res, pts, list(infos), self)
        except ImportError as e:
            self._warn('Job plan', 'The graph needs the Python package matplotlib, which is not installed in this environment.\n\n'
                       'Install it for exactly this Python by running in a terminal (Anaconda Prompt / cmd):\n\n    "' + sys.executable + '" -m pip install matplotlib\n\nThen restart the software.\n\nDetails: ' + str(e))
            return res
        except Exception:
            self._say('Internal error while drawing the plan:\n' + traceback.format_exc())
            return res
        self.plan_dialog = dlg
        dlg.show()
        return res

    def toggle_job(self, pressed):
        if not pressed:
            if not self._confirm_abort():
                if self.running:
                    self.btn_start.setChecked(True)      # keep the job running (button stays 'Abort job')
                return
            if not self.running:
                return                                   # the job ended while the question was open
            self.append_log('abort requested ...')
            self.mpc.request_abort_aquisition()
            # stability: no new start until the job has really ended (Start during the stop crashed the program)
            self.btn_start.setEnabled(False)
            self.btn_start.setText('Aborting ...')
            self.btn_pause.setEnabled(False)
            return
        if self.running or self.mpc.acquisition_running():
            self.btn_start.setChecked(False)
            self._warn('Job', 'The previous acquisition is still running or stopping. Please wait until it has ended.')
            return
        saving_dir = self.lineEdit_savingDir.text().strip()
        if not saving_dir or not os.path.isdir(saving_dir):
            self.btn_start.setChecked(False)
            self._warn('Saving directory', 'Please choose an existing saving directory.\n' + saving_dir)
            return
        res = self.check_job()
        if res is None or res.status != 'completed':
            self.btn_start.setChecked(False)
            self._warn('Job', 'The job cannot be started:\n\n' + (res.error if res is not None and res.error else 'see the message in the JOBs tab'))
            return
        used = list(res.stats.get('per_channel', {}))
        from control.acq_summary import confirm_start
        if not confirm_start(self, 'job', self.mpc, used or [c.name for c in self.configurationManager.configurations],
                             extra=f'{len(self._points())} positions; the job will take an estimated {res.duration_s / 60:.0f} min and take {res.stats.get("images", 0)} images.'):
            self.btn_start.setChecked(False)
            self.label_status.setText('Job not started (cancelled).')
            return
        self._save_cache()
        # same preparation as a standard acquisition
        self.mpc.set_base_path(saving_dir)
        self.mpc.set_selected_configurations(used or [c.name for c in self.configurationManager.configurations])
        self.mpc.set_gen_focus_map_flag(False)
        self.mpc.start_new_experiment(self.lineEdit_experimentID.text().strip() or 'job')
        self.running = True
        self.btn_start.setText('Abort job')
        self._set_controls_enabled(False)
        self.signal_acquisition_started.emit(True)
        self.label_status.setText('Job running ...')
        self.append_log(f'--- job started, experiment folder {self.mpc.experiment_ID} ---')
        self.live.begin(res.events, res.duration_s, self._points())
        self._n_points_at_start = len(self._points())
        self.paused = False
        self.btn_pause.setText('Pause job')
        self.btn_pause.setEnabled(True)
        self._save_cache()
        self.mpc.job_owner = 'jobs'
        self.mpc.run_job(self.lineEdit_job.text().strip(), self._points(), save_as=self.combo_save_as.currentData())

    def _confirm_abort(self):
        """Ask before aborting a running job (not asked if SQUID_SKIP_CONFIRM is set, as in the automated tests)."""
        if os.environ.get('SQUID_SKIP_CONFIRM') or not self.running:
            return True
        box = QMessageBox.question(self, 'Abort job', 'Abort the running job?\n\nImages already taken are kept, but the job cannot be continued afterwards.',
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return box == QMessageBox.Yes

    # --- pause / resume
    def toggle_pause(self):
        if not self.running:
            return
        if not self.paused:
            self.btn_pause.setEnabled(False)
            self.btn_pause.setText('Pausing ...')
            self.label_status.setText('Pause requested: the job stops at the next safe point (between two operations) ...')
            self.mpc.request_job_pause()
            return
        pw = self.positionsWidget
        new = [tuple(float(v) for v in r) for r in pw.location_list]
        if len(new) != self._n_points_at_start:
            self._warn('Resume job', f'The job started with {self._n_points_at_start} positions, the table now has {len(new)}.\n'
                                     f'Positions cannot be added or removed while a job is paused. Restore the original number of positions '
                                     f'(Undo in the Positions window) and resume again.')
            return
        self.append_log('resuming job ...')
        self._set_paused(False)
        self.mpc.resume_job(new)

    def _set_paused(self, paused):
        if paused == self.paused:
            return
        self.paused = paused
        pw = self.positionsWidget
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText('Resume job' if paused else 'Pause job')
        try:
            (pw.disable_the_start_aquisition_button if paused else pw.enable_the_start_aquisition_button)()
        except Exception:
            pass
        if paused:
            self.label_status.setText('Job PAUSED. Move the stage / edit positions in the Flexible Multipoint tab, then press "Resume job".')
        else:
            self.label_status.setText('Job running ...')
        self.signal_job_paused.emit(paused)

    def _on_job_status(self, msg):
        if isinstance(msg, dict) and msg.get('type') == 'state' and msg.get('state') == 'paused' and self.running:
            self._set_paused(True)

    def acquisition_is_finished(self):
        if not self.running:
            return          # a standard acquisition of another tab finished
        self.running = False
        if self.paused:
            self._set_paused(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText('Pause job')
        res = self.mpc.job_result
        if self.live.model.active:            # the runner ended without a final message (crash)
            self.live.on_message(dict(type='finished', status=res.status if res is not None else 'failed', error=res.error if res is not None else 'see the console'))
        self.btn_start.setChecked(False)
        self.btn_start.setText('Start job')
        self.btn_start.setEnabled(True)
        self._set_controls_enabled(True)
        self.signal_acquisition_started.emit(False)
        if res is not None:
            self.append_log('--- job finished ---\n' + describe(res))
            self.label_status.setText('Job ' + res.status + ((': ' + res.error.splitlines()[0]) if res.error else ''))
        else:
            self.label_status.setText('Job ended')

    def _set_controls_enabled(self, enabled):
        for w in (self.lineEdit_job, self.btn_select_job, self.btn_example, self.lineEdit_savingDir, self.btn_savingDir,
                  self.lineEdit_experimentID, self.btn_check, self.btn_plan, self.combo_save_as):
            w.setEnabled(enabled)
