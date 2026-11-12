"""JOBs tab (simulation, offscreen): a job that is longer than the dry-run limit is NOT refused (warning only, "only partly
tested") and then runs completely, because the real run has no operation limit.

  python tests/e2e_jobs_dry_run_limit.py
The limit is lowered to 30 operations for the test (the real one is 200000, see DRY_RUN_MAX_EVENTS in control/jobs/runner.py).
"""
import os, sys, glob, json, time, tempfile, textwrap
SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(SW); sys.path.insert(0, SW)
os.environ.update(QT_API='pyqt5', SQUID_SKIP_CONFIRM='1', QT_QPA_PLATFORM='offscreen')
from unittest.mock import MagicMock
for m in ['napari', 'napari.utils', 'napari.utils.colormaps']: sys.modules[m] = MagicMock()
import control._def as d
d.USE_NAPARI_FOR_LIVE_VIEW = d.USE_NAPARI_FOR_MULTIPOINT = d.USE_NAPARI_FOR_TILED_DISPLAY = False
from qtpy.QtCore import QEventLoop, QTimer
from qtpy.QtWidgets import QApplication, QMessageBox
import control.gui_hcs as gui
import control.jobs.runner as runner

popups = []
QMessageBox.warning = staticmethod(lambda *a, **k: popups.append(str(a[2])))
app = QApplication([]); app.setStyle('Fusion')
win = gui.OctopiGUI(is_simulation=True); win.show(); app.processEvents()
nav, mp, mpc, jw = win.navigationController, win.multiPointWidget2, win.multipointController, win.jobsWidget
res_list = []
def rec(name, ok, detail=''):
    res_list.append(ok); print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail and not ok else ''), flush=True)

save = tempfile.mkdtemp(prefix='limit_e2e_')
mp.clear(); app.processEvents()
for i in range(3):
    nav.move_x_to(1 + i * 0.5); nav.microcontroller.wait_till_operation_is_completed(); nav.update_pos(nav.microcontroller)
    mp.add_location(); app.processEvents()
ch = [c.name for c in win.configurationManager.configurations if not any(u in c.name for u in ('RGB', 'USB Spectrometer'))][0]
job = os.path.join(save, 'job.py')
open(job, 'w').write(textwrap.dedent(f'''
    def run_job(scope):
        for t in scope.timepoints(12, interval=0.3):
            for pt in scope.points():
                scope.move_to(pt)
                scope.capture({ch!r})
'''))    # 12 x 3 x 2 = 72 operations
runner.DRY_RUN_MAX_EVENTS = 30               # as if the job were longer than 200000 operations
win.recordTabWidget.setCurrentWidget(jw)
jw.lineEdit_job.setText(job); jw.lineEdit_savingDir.setText(save); jw.lineEdit_experimentID.setText('limit')

r = jw.check_job()
rec('dry run hits the limit but is not a failure', r is not None and r.status == 'completed' and r.incomplete, getattr(r, 'status', None))
rec('status line says only partly tested', 'only the first part' in jw.label_status.text(), jw.label_status.text())
rec('log shows the warning', 'could not be tested' in jw.log.toPlainText())

loop = QEventLoop(); mpc.acquisitionFinished.connect(loop.quit); QTimer.singleShot(120000, loop.quit)
jw.btn_start.click()
rec('start is NOT refused', jw.running and not popups, popups)
if jw.running:
    loop.exec_()
time.sleep(0.5); app.processEvents()
dirs = glob.glob(os.path.join(save, 'limit_*')); exp = max(dirs, key=os.path.getmtime) if dirs else None
meta = json.load(open(os.path.join(exp, 'job_meta.json'))) if exp and os.path.exists(os.path.join(exp, 'job_meta.json')) else {}
rec('real run completed', meta.get('status') == 'completed', meta.get('status'))
rec('real run did ALL 36 images (no limit)', meta.get('stats', {}).get('images') == 36, meta.get('stats'))
rec('real run has no dry-run warning', not any('could not be tested' in w for w in meta.get('warnings', [])), meta.get('warnings'))
print(f'{sum(res_list)} passed, {len(res_list) - sum(res_list)} failed')
os._exit(0 if all(res_list) else 1)
