"""End-to-end test of the JOBs tab (simulation or real microscope).

Starts the real GUI, defines positions with group IDs in the Flexible Multipoint tab, then runs real jobs through the
JOBs tab with the real MultiPointWorker: (1) a time-lapse job with two channels and staggered autofocus,
(2) an abort in the middle of a job, (3) a broken job that must be refused, (4) a standard acquisition afterwards
(proves the standard mode is unchanged). Checks images, metadata files, scheduling and cleanup.

  Simulation (no hardware):   python tests/e2e_jobs.py --simulation --save-dir C:\\temp\\squid_jobs
  Real microscope (MOVES THE STAGE between positions, runs autofocus, illumination on):
                              python tests/e2e_jobs.py --hardware --save-dir D:\\test_runs --start-x 20 --start-y 20 --start-z 3.1

Exit code 0 = all checks passed. Writes jobs_e2e_report_<time>.txt into --save-dir.
"""
import os
import numpy as np
import sys
import csv
import json
import glob
import time
import argparse
import tempfile
import textwrap
import traceback

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--simulation', action='store_true')
    mode.add_argument('--hardware', action='store_true', help='REAL microscope: moves the stage')
    ap.add_argument('--save-dir', required=True)
    ap.add_argument('--start-x', type=float)
    ap.add_argument('--start-y', type=float)
    ap.add_argument('--start-z', type=float)
    ap.add_argument('--spacing-mm', type=float, default=0.5)
    ap.add_argument('--n-positions', type=int, default=4)
    ap.add_argument('--timeout-s', type=float, default=300)
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if not os.path.isdir(args.save_dir):
        sys.exit('Saving directory does not exist: ' + args.save_dir)

    os.chdir(SOFTWARE_DIR)
    sys.path.insert(0, SOFTWARE_DIR)
    os.environ['QT_API'] = 'pyqt5'
    os.environ['SQUID_SKIP_CONFIRM'] = '1'      # no confirmation window (automated run)
    if args.simulation and os.environ.get('SQUID_E2E_OFFSCREEN'):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from unittest.mock import MagicMock
        for m in ['napari', 'napari.utils', 'napari.utils.colormaps']:
            sys.modules[m] = MagicMock()
        import control._def as d
        d.USE_NAPARI_FOR_LIVE_VIEW = d.USE_NAPARI_FOR_MULTIPOINT = d.USE_NAPARI_FOR_TILED_DISPLAY = False
    from qtpy.QtCore import QEventLoop, QTimer
    from qtpy.QtWidgets import QApplication, QMessageBox
    import control.gui_hcs as gui

    popups = []
    QMessageBox.warning = staticmethod(lambda *a, **k: popups.append(str(a[2])))

    app = QApplication([])
    app.setStyle('Fusion')
    win = gui.OctopiGUI(is_simulation=args.simulation)
    win.show()
    app.processEvents()
    nav, mp, mpc, jw = win.navigationController, win.multiPointWidget2, win.multipointController, win.jobsWidget

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    def refresh_position():
        nav.microcontroller.wait_till_operation_is_completed()
        nav.update_pos(nav.microcontroller)
        app.processEvents()

    def move_to(x, y, z):
        nav.move_x_to(x); nav.move_y_to(y); nav.move_z_to(z)
        pump(0.2)
        refresh_position()

    refresh_position()
    start = (args.start_x if args.start_x is not None else nav.x_pos_mm,
             args.start_y if args.start_y is not None else nav.y_pos_mm,
             args.start_z if args.start_z is not None else nav.z_pos_mm)
    channels = [c.name for c in win.configurationManager.configurations
                if not any(u in c.name for u in ('RGB', 'USB Spectrometer'))][:2]
    print('=' * 70)
    print('mode:', 'REAL HARDWARE' if args.hardware else 'simulation', ' channels:', channels, ' start:', start)
    print('=' * 70)
    if args.hardware and not args.yes:
        span = args.spacing_mm * (args.n_positions - 1)
        print(f'The stage will move along x from {start[0]} to {start[0] + span} mm and autofocus will run.')
        if input('Type MOVE to continue: ').strip() != 'MOVE':
            sys.exit('Aborted.')

    results = []

    def record(name, ok, detail=''):
        results.append((name, bool(ok), detail))
        print(('  PASS  ' if ok else '  FAIL  ') + name + (f'   [{detail}]' if detail and not ok else ''))

    work = tempfile.mkdtemp(prefix='jobs_e2e_')

    def write_job(name, body):
        p = os.path.join(work, name)
        open(p, 'w').write(textwrap.dedent(body))
        return p

    def run_job_tab(job_path, exp_id, wait_abort_after=None):
        win.recordTabWidget.setCurrentWidget(jw)      # the user starts a job from the JOBs tab; other tabs are locked while it runs
        jw.lineEdit_job.setText(job_path)
        jw.lineEdit_savingDir.setText(args.save_dir)
        jw.lineEdit_experimentID.setText(exp_id)
        loop = QEventLoop()
        mpc.acquisitionFinished.connect(loop.quit)
        QTimer.singleShot(int(args.timeout_s * 1000), loop.quit)
        t0 = time.time()
        jw.btn_start.click()
        if not jw.running:
            mpc.acquisitionFinished.disconnect(loop.quit)
            return None, False
        if wait_abort_after is not None:
            QTimer.singleShot(int(wait_abort_after * 1000), jw.btn_start.click)   # second click = abort
        loop.exec_()
        finished = (time.time() - t0) < args.timeout_s - 1
        try:
            mpc.acquisitionFinished.disconnect(loop.quit)
        except TypeError:
            pass
        pump(1)
        dirs = [d for d in glob.glob(os.path.join(args.save_dir, exp_id + '_*')) if os.path.isdir(d)]
        return (max(dirs, key=os.path.getmtime) if dirs else None), finished

    def rows(d, name):
        p = os.path.join(d, name + '.csv')
        return list(csv.DictReader(open(p))) if os.path.exists(p) else []

    try:
        # --- positions with group IDs in the Flexible Multipoint tab
        mp.clear(); app.processEvents()
        n = args.n_positions
        ids = [('A-%d' % i) if i < n // 2 else ('B-%d' % (i - n // 2)) for i in range(n)]
        for i in range(n):
            move_to(start[0] + i * args.spacing_mm, start[1], start[2])
            mp.add_location()
            app.processEvents()
        for i, pid in enumerate(ids):
            mp.location_ids[i] = pid
        record('positions with IDs defined', len(mp.location_list) == n)
        jw.refresh_points_label()
        record('JOBs tab shows the groups', 'A (' in jw.label_points.text() and 'B (' in jw.label_points.text(), jw.label_points.text())

        # --- 1. time-lapse job: 3 time points, 2 channels, staggered autofocus every 2nd, group A only for channel 2
        ch1, ch2 = channels
        job1 = write_job('job1.py', f'''
            def run_job(scope):
                for t in scope.timepoints(3, interval=2):
                    for pt in scope.points():
                        scope.move_to(pt)
                        if (t.index + pt.index) % 2 == 0:
                            scope.autofocus()
                        scope.capture({ch1!r})
                        if pt.group == "A":
                            scope.capture({ch2!r})
        ''')
        jw.lineEdit_job.setText(job1)
        res = jw.check_job()
        record('dry run of job 1 completes', res is not None and res.status == 'completed', str(res and res.error))
        doc = jw.show_documentation()
        record('opening a job shows its documentation', doc is not None and '=== job1.py' in doc, str(doc)[:80])
        plan = jw.show_plan()
        record('job plan graph opens', plan is not None and plan.status == 'completed' and len(getattr(jw, 'plan_dialog').canvas.figure.axes) >= 5)
        jw.plan_dialog.close()
        exp_images = 3 * n + 3 * (n // 2)
        record('dry run counts the images', res is not None and res.stats['images'] == exp_images, str(res and res.stats.get('images')))
        d, finished = run_job_tab(job1, 'jobs_e2e_1')
        record('job 1 finished before timeout', finished)
        record('job 1 experiment folder created', d is not None)
        if d:
            meta = json.load(open(os.path.join(d, 'job_meta.json')))
            record('job 1 status completed', meta['status'] == 'completed', str(meta.get('error')))
            im = rows(d, 'images')
            record('job 1 image count', len(im) == exp_images, f'{len(im)} vs {exp_images}')
            record('job 1 all image files exist', all(os.path.isfile(os.path.join(d, r['file'])) for r in im))
            record('job 1 one multipage file per point and time point (Time0000_Point0000.tiff, ...)', len({r['file'] for r in im}) == 3 * n and im[0]['file'] == 'Time0000_Point0000.tiff', str(sorted({r['file'] for r in im})[:3]))
            record('job 1 images carry the row number as ID and the groups', {r['group'] for r in im} == {'A', 'B'} and im[0]['point_id'] == '0' and all(r['point_id'] == r['point_index'] for r in im))
            af = rows(d, 'autofocus')
            exp_af = sum(1 for t in range(3) for p in range(n) if (t + p) % 2 == 0)
            record('job 1 autofocus count (staggered)', len(af) == exp_af, f'{len(af)} vs {exp_af}')
            record('job 1 job copy and positions saved', all(os.path.isfile(os.path.join(d, f)) for f in ('job.py', 'positions_initial.csv', 'positions_final.csv', 'job_log.txt')))
            starts = sorted({round(float(r['time_s'])) for r in im if r['point_index'] == '0' and r['channel'] == ch1})
            record('job 1 time points start on the interval grid', len(starts) == 3 and (starts[1] - starts[0]) >= 2, str(starts))
            try:
                import tifffile
                a = tifffile.imread(os.path.join(d, im[0]['file']))
                record('job 1 image readable (OME-TIFF)', a.ndim in (2, 3) and a.size > 0, str(a.shape))
            except Exception as e:
                record('job 1 image readable (OME-TIFF)', False, repr(e))
        lm = jw.live.model
        record('live view: job 1 finished state', lm.status == 'completed' and not lm.active, str(lm.status))
        record('live view: images counted', lm.images_done == exp_images and lm.plan_total_images == exp_images, f'{lm.images_done}/{lm.plan_total_images}')
        record('live view: all time points done', [st for _, st in lm.tp_boxes()] == ['done'] * 3, str(lm.tp_boxes()))
        record('illumination off after job 1', not win.liveController.illumination_on)
        record('start button reset after job 1', (not jw.btn_start.isChecked()) and jw.btn_start.text() == 'Start job')

        # --- 2. abort in the middle of a long job
        job2 = write_job('job2.py', f'''
            def run_job(scope):
                for t in scope.timepoints(1000, interval=1):
                    for pt in scope.points():
                        scope.move_to(pt)
                        scope.capture({ch1!r})
        ''')
        d2, finished = run_job_tab(job2, 'jobs_e2e_2', wait_abort_after=6)
        record('job 2 stopped by abort (before timeout)', finished)
        if d2:
            meta = json.load(open(os.path.join(d2, 'job_meta.json')))
            record('job 2 status aborted', meta['status'] == 'aborted', meta['status'])
            record('job 2 took some images', len(rows(d2, 'images')) > 0)
        record('live view: aborted state', jw.live.model.status == 'aborted', str(jw.live.model.status))
        record('illumination off after abort', not win.liveController.illumination_on)
        record('stage controls usable after abort', not nav.microcontroller.is_busy())

        # --- 2b. pause, edit a position, resume, abort
        win.recordTabWidget.setCurrentWidget(jw)
        jw.lineEdit_job.setText(job2); jw.lineEdit_savingDir.setText(args.save_dir); jw.lineEdit_experimentID.setText('jobs_e2e_pause')
        loop = QEventLoop()
        mpc.acquisitionFinished.connect(loop.quit)
        QTimer.singleShot(int(args.timeout_s * 1000), loop.quit)
        jw.btn_start.click()
        record('pause button enabled while a job runs', jw.running and jw.btn_pause.isEnabled())
        pump(4)
        jw.btn_pause.click()
        tw = time.time()
        while not jw.paused and time.time() - tw < 60:
            pump(0.2)
        record('job paused at a safe point', jw.paused and jw.btn_pause.text() == 'Resume job')
        n1 = jw.live.model.images_done
        pump(3)
        record('no images are taken while paused', jw.live.model.images_done == n1, f'{n1} -> {jw.live.model.images_done}')
        record('live view says PAUSED', 'PAUSED' in jw.live.label_head.text(), jw.live.label_head.text())
        tabs = {win.recordTabWidget.widget(i): win.recordTabWidget.isTabEnabled(i) for i in range(win.recordTabWidget.count())}
        record('while paused: Flexible Multipoint + JOBs tabs usable, others locked',
               tabs[mp] and tabs[jw] and not any(v for w, v in tabs.items() if w is not mp and w is not jw), str(list(tabs.values())))
        record('while paused: start button of Flexible Multipoint disabled', not mp.btn_startAcquisition.isEnabled())
        record('while paused: stage controls enabled', win.navigationWidget.btn_moveX_forward.isEnabled())
        # edit position 0 slightly (as a user would after re-focusing), refuse a changed count, then resume
        mp.location_list[0][2] = mp.location_list[0][2] + 0.0005
        z_new = float(mp.location_list[0][2])
        popups.clear()
        mp.location_list = mp.location_list[:-1]
        jw.btn_pause.click()
        record('resume refused when the number of positions changed', jw.paused and len(popups) == 1, str(popups))
        mp.location_list = np.vstack([mp.location_list, [start[0], start[1], start[2]]])
        jw.btn_pause.click()
        record('job resumed', not jw.paused and jw.btn_pause.text() == 'Pause job')
        n2 = jw.live.model.images_done
        pump(6)
        record('images are taken again after resume', jw.live.model.images_done > n2, f'{n2} -> {jw.live.model.images_done}')
        record('tabs locked again after resume (Flexible Multipoint tab and its start button)', not win.recordTabWidget.isTabEnabled(win.recordTabWidget.indexOf(mp)) and not mp.btn_startAcquisition.isEnabled())
        jw.btn_start.click()               # abort
        loop.exec_()
        try:
            mpc.acquisitionFinished.disconnect(loop.quit)
        except TypeError:
            pass
        pump(1)
        record('job aborted after resume', jw.live.model.status == 'aborted', str(jw.live.model.status))
        record('after the job: all tabs usable, Flexible start button enabled, pause button off',
               all(win.recordTabWidget.isTabEnabled(i) for i in range(win.recordTabWidget.count())) and mp.btn_startAcquisition.isEnabled() and not jw.btn_pause.isEnabled())
        dp = [x for x in glob.glob(os.path.join(args.save_dir, 'jobs_e2e_pause_*')) if os.path.isdir(x)]
        if dp:
            dpm = max(dp, key=os.path.getmtime)
            log = open(os.path.join(dpm, 'job_log.txt'), encoding='utf-8').read()
            record('job log shows pause and resume', 'job PAUSED' in log and 'job RESUMED' in log)
            record('job log shows the changed position', 'changed while the job was paused' in log)

        # --- 2b. z-stack and movie through the real hardware backend (simulation), lossless and lossy
        job_zs = write_job('job_zs.py', f'''
            def run_job(scope):
                pt = scope.points()[0]
                scope.move_to(pt)
                z0 = scope.zstack({[ch1, ch2]!r}, planes=3, step_um=2.0, order="z_major")
                scope.movie({ch1!r}, frames=3, interval_s=0.6, compression="lossy")
                scope.capture({ch1!r}, compression="lossy")
        ''')
        res = jw.check_job()
        d3, finished = run_job_tab(job_zs, 'jobs_e2e_zs')
        record('z-stack/movie job finished', finished and d3 is not None)
        if d3:
            meta = json.load(open(os.path.join(d3, 'job_meta.json')))
            record('z-stack/movie job status completed', meta['status'] == 'completed', str(meta.get('error')))
            fr = rows(d3, 'frames')
            record('frames.csv: 6 stack pages + 3 movie frames', len(fr) == 9, str(len(fr)))
            zs_rows = [r for r in fr if r['kind'] == 'zstack']
            zz = sorted({round(float(r['z_mm']) * 1000, 1) for r in zs_rows})
            record('z planes 2 um apart', len(zz) == 3 and abs((zz[1] - zz[0]) - 2.0) < 0.6 and abs((zz[2] - zz[1]) - 2.0) < 0.6, str(zz))
            try:
                import tifffile
                files = sorted({r['file'] for r in fr})
                with tifffile.TiffFile(os.path.join(d3, [f for f in files if 'zstack' in f.lower()][0])) as tf:
                    record('z-stack file: 6 pages, lossless, OME', len(tf.pages) == 6 and tf.pages[0].compression.name == 'ADOBE_DEFLATE' and tf.is_ome)
                with tifffile.TiffFile(os.path.join(d3, [f for f in files if 'movie' in f.lower()][0])) as tf:
                    record('movie file: 3 pages, JPEG2000, OME with times', len(tf.pages) == 3 and tf.pages[0].compression.name == 'JPEG2000' and 'DeltaT=' in tf.ome_metadata)
                    tms = [float(r['dt_s']) for r in fr if r['kind'] == 'movie']
                    record('movie frames about 0.6 s apart', len(tms) == 3 and all(abs(b - a - 0.6) < 0.25 for a, b in zip(tms, tms[1:])), str(tms))
            except Exception as e:
                record('z-stack/movie files readable', False, repr(e))
            ims = rows(d3, 'images')
            record('single lossy capture listed', len(ims) == 1)
        record('illumination off after z-stack/movie job', not win.liveController.illumination_on)

        # --- 3. broken jobs are refused
        popups.clear()
        _, _ = run_job_tab(write_job('bad1.py', 'def run_job(scope)\n    pass\n'), 'jobs_e2e_bad1')
        record('syntax error: job refused with a message', len(popups) == 1 and 'syntax error' in popups[0], str(popups))
        popups.clear()
        _, _ = run_job_tab(write_job('bad2.py', 'def run_job(scope):\n    scope.capture("no such channel")\n'), 'jobs_e2e_bad2')
        record('unknown channel: job refused with a message', len(popups) == 1 and 'unknown channel' in popups[0], str(popups))
        record('start button reset after refused job', not jw.btn_start.isChecked() and not jw.running)

        # --- 4. the standard acquisition still works and is not hijacked by the earlier jobs
        record('standard mode: no leftover job file', True)
        win.recordTabWidget.setCurrentWidget(mp)
        mp.lineEdit_savingDir.setText(args.save_dir)
        mpc.set_base_path(args.save_dir)
        mp.lineEdit_experimentID.setText('jobs_e2e_std')
        mp.entry_NX.setValue(1); mp.entry_NY.setValue(1); mp.entry_NZ.setValue(1)
        mp.entry_Nt.setValue(1); mp.entry_dt.setValue(0)
        from qtpy.QtCore import Qt
        mp.list_configurations.clearSelection()
        for name in channels:
            for item in mp.list_configurations.findItems(name, Qt.MatchExactly):
                item.setSelected(True)
        mp.checkbox_withAutofocus.setChecked(False)
        mp.checkbox_save_multichannel.setChecked(False)
        loop = QEventLoop()
        mpc.acquisitionFinished.connect(loop.quit)
        QTimer.singleShot(int(args.timeout_s * 1000), loop.quit)
        mp.btn_startAcquisition.click()
        loop.exec_()
        try:
            mpc.acquisitionFinished.disconnect(loop.quit)
        except TypeError:
            pass
        pump(1)
        dirs = [x for x in glob.glob(os.path.join(args.save_dir, 'jobs_e2e_std_*')) if os.path.isdir(x)]
        std = max(dirs, key=os.path.getmtime) if dirs else None
        record('standard acquisition ran', std is not None)
        if std:
            imgs = [f for f in glob.glob(os.path.join(std, '0', '*')) if f.lower().endswith(('.tiff', '.tif', '.bmp', '.png', '.jpg'))]
            record('standard acquisition images (positions x channels)', len(imgs) == n * len(channels), f'{len(imgs)} vs {n * len(channels)}')
            record('standard acquisition made no job files', not os.path.exists(os.path.join(std, 'job_meta.json')))
        record('controller has no job set after a standard run', mpc.job_file is None)
    except Exception:
        record('test ran without exception', False, traceback.format_exc().splitlines()[-1])
        traceback.print_exc()

    try:
        move_to(*start)
    except Exception:
        pass
    n_fail = sum(1 for r in results if not r[1])
    report = os.path.join(args.save_dir, 'jobs_e2e_report_' + time.strftime('%Y%m%d_%H%M%S') + '.txt')
    with open(report, 'w', encoding='utf-8') as f:
        f.write(f'mode: {"hardware" if args.hardware else "simulation"}\nstart: {start}\nchannels: {channels}\n\n')
        for name, ok, detail in results:
            f.write(('PASS  ' if ok else 'FAIL  ') + name + (f'   [{detail}]' if detail and not ok else '') + '\n')
        f.write(f'\n{len(results) - n_fail} passed, {n_fail} failed\n')
    print(f'\n{len(results) - n_fail} passed, {n_fail} failed. Report: {report}')
    sys.stdout.flush()
    os._exit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
