"""Tests of the JOBs scripting core (scheduling, files, abort, errors, groups) without hardware and Qt.

Run:  python tests/test_jobs_api.py      (or: python -m pytest tests)
"""
import os
import sys
import csv
import json
import shutil
import tempfile
import textwrap

import numpy as np

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOFTWARE_DIR)
from control.jobs.api import make_points, SimClock, Backend, JobAborted  # noqa: E402
from control.jobs.runner import run_job, dry_run, describe  # noqa: E402

CHANNELS = {'BF LED matrix full': dict(exposure_ms=10.0, gain=0.0, intensity=20.0),
            'Fluorescence 488 nm Ex': dict(exposure_ms=10.0, gain=0.0, intensity=50.0),
            'Fluorescence 561 nm Ex': dict(exposure_ms=10.0, gain=0.0, intensity=50.0)}


class TestBackend(Backend):
    """Simulated microscope that writes small real files; can abort after N captures."""
    def __init__(self, clock, abort_after=None, capture_s=0.5, move_s=1.0, af_z_shift=0.0, channels=None):
        self.clock, self.abort_after, self.capture_s, self.move_s, self.af_z_shift = clock, abort_after, capture_s, move_s, af_z_shift
        self._channels = channels or CHANNELS
        self.n_captures = 0
        self.cleaned = False
        self.pos = (0, 0, 0)
        self.moves, self.messages = [], []
        self.af_calls = []

    def channels(self): return list(self._channels)
    def move_to(self, x, y, z): self.clock.sleep(self.move_s); self.pos = (x, y, z); self.moves.append((x, y, z))
    def position(self): return self.pos

    def capture(self, channel):
        self.clock.sleep(self.capture_s)
        self.n_captures += 1
        info = dict(self._channels[channel]); info.update(x_mm=self.pos[0], y_mm=self.pos[1], z_mm=self.pos[2])
        return np.full((4, 4), self.n_captures, dtype=np.uint16), info

    def autofocus(self, channel=None, focus_measure=None):
        self.clock.sleep(5)
        self.af_calls.append((channel, focus_measure))
        self.pos = (self.pos[0], self.pos[1], self.pos[2] + self.af_z_shift)
        return self.pos[2]

    def save_image(self, image, path, channel):
        np.save(path + '.npy', image); open(path, 'wb').write(b'x')

    def abort_requested(self):
        return self.abort_after is not None and self.n_captures >= self.abort_after

    def log(self, m): self.messages.append(m)
    def cleanup(self): self.cleaned = True


def make_job(text):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'job.py')
    open(p, 'w').write(textwrap.dedent(text))
    return p


def pts(n, ids=None):
    ids = ids or [f'A-{i}' for i in range(n)]
    return make_points(ids, [(i * 1.0, 2.0, 3.0) for i in range(n)])


def run(job_text, points, **kw):
    clock = SimClock()
    be = TestBackend(clock, **{k: kw.pop(k) for k in list(kw) if k in ('abort_after', 'capture_s', 'move_s', 'af_z_shift')})
    out = tempfile.mkdtemp()
    res = run_job(make_job(job_text), points, be, experiment_dir=out, clock=clock, **kw)
    return res, be, out


def rows(out, name):
    p = os.path.join(out, name + '.csv')
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def test_groups_and_ids():
    p = make_points(['A-0', 'A-1', 'B-0', '', 'A-2'], [(0, 0, 0)] * 5)
    assert [x.group for x in p] == ['A', 'A', 'B', 'default', 'A']
    assert [x.number for x in p] == [0, 1, 0, 0, 2]
    assert [x.index for x in p] == [0, 1, 2, 3, 4]


def old_standard_name(row, channel, k=0):
    """File name (without the time point folder) of the previous software's standard Flexible Multipoint acquisition:
    core.py MultiPointWorker: name = str(row number) + '_', file_ID = name + i + '_' + j + '_' + k, file = file_ID + '_' + channel(' ' -> '_') + '.tiff'"""
    file_id = str(row) + '_' + str(0) + '_' + str(0) + '_' + str(k)
    return file_id + '_' + channel.replace(' ', '_') + '.tiff'


def test_single_file_names_and_csv_id_columns_use_the_row_number():
    # groups 1, 1a, 2, 2a with two positions each; the table stores '1-0', '1-1', '1a-0', ... ("Set group(s)" with "add number")
    ids = ['1-0', '1-1', '1a-0', '1a-1', '2-0', '2-1', '2a-0', '2a-1']
    p = make_points(ids, [(i * 1.0, 2.0, 3.0) for i in range(8)])
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=100):
                for g in ("1", "1a", "2", "2a"):                 # groups are referred to by name, as before
                    for pt in scope.points(g):
                        scope.move_to(pt)
                        if pt.number == 0:
                            scope.autofocus()
                        scope.capture("BF")
    ''', p, save_as='single')
    assert res.status == 'completed', res.error
    assert [pt.id for pt in p][:3] == ['1-0', '1-1', '1a-0']                       # the points themselves are unchanged
    assert [pt.group for pt in p] == ['1', '1', '1a', '1a', '2', '2', '2a', '2a'] and [pt.number for pt in p] == [0, 1] * 4
    im = rows(out, 'images')
    assert len(im) == 16
    for r in im:
        row = int(r['point_index'])
        assert r['point_id'] == str(row)                                            # ID column = row number, point_index unchanged
        assert r['group'] == ['1', '1', '1a', '1a', '2', '2', '2a', '2a'][row]      # group = what was typed
        assert r['file'].replace('\\', '/') == f"{r['timepoint']}/" + old_standard_name(row, 'BF LED matrix full'), r['file']
    assert sorted({r['file'].replace('\\', '/') for r in im if r['timepoint'] == '0'}) == sorted(f'0/{i}_0_0_0_BF_LED_matrix_full.tiff' for i in range(8))
    assert all(os.path.isfile(os.path.join(out, r['file'])) for r in im)
    for table in ('positions_initial', 'positions_final'):
        pr = rows(out, table)
        assert [r['index'] for r in pr] == [str(i) for i in range(8)] and [r['id'] for r in pr] == [str(i) for i in range(8)]
        assert [r['group'] for r in pr] == ['1', '1', '1a', '1a', '2', '2', '2a', '2a'] and [r['number_in_group'] for r in pr] == ['0', '1'] * 4
    af = rows(out, 'autofocus')
    assert len(af) == 8 and list(af[0])[-1] == 'group'                               # new column, appended at the end
    assert [r['point_id'] for r in af[:4]] == ['0', '2', '4', '6'] and [r['group'] for r in af[:4]] == ['1', '1a', '2', '2a']
    assert all(r['point_id'] == r['point_index'] for r in af)


def test_loop_files_and_metadata():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(3, interval=100):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.capture("BF")
                    scope.capture("488")
    ''', pts(2), save_as='single')
    assert res.status == 'completed', res.error
    assert res.stats['images'] == 12 and res.stats['per_channel'] == {'BF LED matrix full': 6, 'Fluorescence 488 nm Ex': 6}
    r = rows(out, 'images')
    assert len(r) == 12 and len({x['file'] for x in r}) == 12      # unique file names
    assert all(os.path.isfile(os.path.join(out, x['file'])) for x in r)
    assert r[0]['point_id'] == '0' and r[0]['timepoint'] == '0' and r[0]['x_mm'] == '0.0'
    assert r[0]['file'].replace('\\', '/') == '0/0_0_0_0_BF_LED_matrix_full.tiff', r[0]['file']      # single files: <time point>/<row number>_0_0_<z>_<channel>.tiff
    for f in ('job.py', 'job_log.txt', 'job_meta.json', 'positions_initial.csv', 'positions_final.csv'):
        assert os.path.isfile(os.path.join(out, f)), f
    meta = json.load(open(os.path.join(out, 'job_meta.json')))
    assert meta['status'] == 'completed' and meta['stats']['images'] == 12
    assert be.cleaned


def test_same_channel_twice_does_not_collide():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            for i in range(3):
                scope.capture("488")
    ''', pts(1))
    files = [x['file'] for x in rows(out, 'images')]
    assert len(set(files)) == 3 and all(os.path.isfile(os.path.join(out, f)) for f in files), files


def test_staggered_autofocus_count():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(10, interval=60):
                for pt in scope.points():
                    scope.move_to(pt)
                    if (t.index + pt.index) % 5 == 0:
                        scope.autofocus()
                    scope.capture("488")
    ''', pts(10))
    assert res.status == 'completed', res.error
    assert res.stats['autofocus'] == 20                                  # 10 points * 10 time points / 5
    af = rows(out, 'autofocus')
    per_t = {}
    for a in af:
        per_t.setdefault(a['timepoint'], []).append(int(a['point_index']))
    assert all(len(v) == 2 for v in per_t.values()), per_t               # 20 % of 10 points per time point


def test_template_staggered_af_bf_gfp_rfp():
    src = open(os.path.join(SOFTWARE_DIR, 'job_examples', '04_staggered_af_bf_gfp_rfp.py')).read()
    n_pts, n_tp = 10, 12
    src = src.replace('N_TIMEPOINTS = 120', f'N_TIMEPOINTS = {n_tp}')
    res, be, out = run(src, pts(n_pts, ids=[f'X{i}' for i in range(n_pts)]))   # IDs are ignored: one group
    assert res.status == 'completed', res.error
    assert all(c == ('Fluorescence 561 nm Ex', 'GLVA') for c in be.af_calls), be.af_calls[:3]
    af = rows(out, 'autofocus')
    per_t = {}
    for a in af:
        per_t.setdefault(int(a['timepoint']), []).append(int(a['point_index']))
    assert sorted(per_t[0]) == list(range(n_pts))                              # first time point: all positions
    for t in range(1, n_tp):                                                   # later: staggered, every 5th time point
        assert sorted(per_t.get(t, [])) == [k for k in range(n_pts) if (t + k) % 5 == 0], (t, per_t.get(t))
    imgs = rows(out, 'images')
    assert len(imgs) == n_pts * n_tp * 3
    assert [x['channel'] for x in imgs[:3]] == ['BF LED matrix full', 'Fluorescence 488 nm Ex', 'Fluorescence 561 nm Ex']
    tp_start = {int(x['timepoint']): float(x['time_s']) for x in imgs if x['point_index'] == '0' and x['channel'] == 'BF LED matrix full'}
    assert all(abs(tp_start[t] - tp_start[0] - 60 * t) < 20 for t in range(n_tp))   # 1 min start to start


def test_interval_start_to_start():
    # one pass takes 10 * (1 + 0.5) = 15 s, interval 60 s: starts at 0, 60, 120
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(3, interval=60):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.capture("488")
    ''', pts(10))
    log = open(os.path.join(out, 'job_log.txt')).read()
    assert 'time point 2 starts' in log and res.stats['late_timepoints'] == 0
    r = [float(x['time_s']) for x in rows(out, 'images') if x['point_index'] == '0']
    assert abs(r[1] - r[0] - 60) < 2 and abs(r[2] - r[1] - 60) < 2, r


def test_late_timepoint_starts_immediately_and_none_skipped():
    # one pass takes 10 * 1.5 = 15 s but interval is 10 s -> every time point starts immediately after the previous
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(4, interval=10):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.capture("488")
    ''', pts(10))
    assert res.stats['timepoints'] == 4 and res.stats['images'] == 40      # none skipped
    assert res.stats['late_timepoints'] == 3 and res.stats['max_late_s'] > 4
    assert 'late' in open(os.path.join(out, 'job_log.txt')).read()
    starts = [float(x['time_s']) for x in rows(out, 'images') if x['point_index'] == '0']
    assert abs((starts[1] - starts[0]) - 15) < 1.0, starts              # started right after the previous pass


def test_abort_cleans_up_and_is_not_swallowed():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(5, interval=10):
                for pt in scope.points():
                    try:
                        scope.move_to(pt)
                        scope.capture("488")
                    except Exception:
                        scope.log("swallowed")
    ''', pts(4), abort_after=6)
    assert res.status == 'aborted' and be.cleaned
    assert be.n_captures == 6
    assert 'swallowed' not in open(os.path.join(out, 'job_log.txt')).read()
    assert json.load(open(os.path.join(out, 'job_meta.json')))['status'] == 'aborted'


def test_errors_are_reported_not_raised():
    res, be, out = run('def run_job(scope):\n    scope.capture("nonexistent")\n', pts(1))
    assert res.status == 'failed' and 'unknown channel' in res.error and 'BF LED matrix full' in res.error and be.cleaned
    res, be, out = run('def other(scope):\n    pass\n', pts(1))
    assert res.status == 'invalid' and 'run_job' in res.error
    res, be, out = run('def run_job(scope)\n    pass\n', pts(1))
    assert res.status == 'invalid' and 'syntax error' in res.error and 'line 1' in res.error
    res, be, out = run('def run_job(scope):\n    x = 1/0\n', pts(1))
    assert res.status == 'failed' and 'ZeroDivisionError' in res.error and 'line 2' in res.error and be.cleaned
    res, be, out = run('def run_job(scope):\n    scope.move_to("A-0")\n', pts(1))
    assert res.status == 'failed' and 'Point' in res.error


def test_channel_matching():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488")
            scope.capture("bf")
            scope.capture("Ex")
    ''', pts(1))
    assert res.status == 'failed' and 'ambiguous' in res.error and res.stats['images'] == 2


def test_groups_in_scope():
    res, be, out = run('''
        def run_job(scope):
            assert set(scope.groups) == {"A", "B"}, scope.groups
            for pt in scope.points("B"):
                scope.move_to(pt)
                scope.capture("488")
            for pt in scope.points("A", "B"):
                assert pt.group in ("A", "B")
            try:
                scope.points("C")
            except Exception as e:
                scope.log("ok: " + str(e))
    ''', pts(4, ['A-0', 'B-0', 'A-1', 'B-1']))
    assert res.status == 'completed', res.error
    assert res.stats['per_group'] == {'B': 2}
    assert [m[0] for m in be.moves] == [1.0, 3.0]


def test_autofocus_updates_point_z_unless_drift_too_large():
    res, be, out = run('''
        def run_job(scope):
            pt = scope.points()[0]
            scope.move_to(pt)
            z = scope.autofocus()
            assert abs(pt.z - z) < 1e-9
            scope.move_to(pt)               # second visit starts at the focused z
    ''', pts(1), af_z_shift=0.1)
    assert res.status == 'completed', res.error
    assert abs(be.moves[1][2] - 3.1) < 1e-9
    points = pts(1)
    res, be, out = run('''
        def run_job(scope):
            pt = scope.points()[0]
            scope.move_to(pt)
            scope.autofocus()
            scope.move_to(pt)
    ''', points, af_z_shift=0.8)
    assert abs(be.moves[1][2] - 3.0) < 1e-9 and any('more than' in w for w in res.warnings), res.warnings


def test_wait_and_wait_until_use_job_time():
    res, be, out = run('''
        def run_job(scope):
            scope.wait(30)
            assert 29.9 < scope.elapsed < 30.5, scope.elapsed
            scope.wait_until(100)
            assert 99.9 < scope.elapsed < 100.5, scope.elapsed
            scope.wait_until(50)             # already later: returns at once
            assert scope.elapsed < 101
    ''', pts(1))
    assert res.status == 'completed', res.error


def test_dry_run_reports_plan_and_warnings():
    job = make_job('''
        def run_job(scope):
            for t in scope.timepoints(6, interval=1800):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.capture("488")
                    if t.index % 2 == 0:
                        scope.capture("BF")
    ''')
    infos = {k: dict(v) for k, v in CHANNELS.items()}
    infos['Fluorescence 488 nm Ex']['exposure_ms'] = 80.0
    res = dry_run(job, pts(5), infos)
    assert res.status == 'completed' and res.stats['images'] == 6 * 5 + 3 * 5
    text = describe(res)
    assert 'images: 45' in text and 'different exposure' in text
    assert 5 * 1800 < res.duration_s < 6 * 1800
    # identical exposures: no warning
    res = dry_run(job, pts(5), CHANNELS)
    assert not any('exposure' in w for w in res.warnings)


def test_dry_run_stops_endless_loop_with_warning():
    # the limit only protects the dry run (simulated time); it is a warning, the job is not refused
    job = make_job('def run_job(scope):\n    while True:\n        scope.wait(1)\n')
    res = dry_run(job, pts(1), CHANNELS)
    assert res.status == 'completed' and res.incomplete and not res.error
    assert any('could not be tested' in w for w in res.warnings), res.warnings
    assert 'only partly tested' in describe(res)


LONG_JOB = '''
def run_job(scope):
    for t in scope.timepoints(120, interval=3600):
        for pt in scope.points():
            scope.move_to(pt)
            scope.capture("488")
            scope.capture("BF")
'''


def test_dry_run_longer_than_limit_is_only_a_warning():
    # lifespan scale: 120 time points x 900 positions x 3 operations = 324000 > 200000 operations
    res = dry_run(make_job(LONG_JOB), pts(900), CHANNELS)
    assert res.status == 'completed' and res.incomplete, (res.status, res.error)
    assert any('could not be tested' in w for w in res.warnings)
    assert 0 < res.stats['images'] < 120 * 900 * 2
    # a short job is unaffected
    res = dry_run(make_job(LONG_JOB.replace('120', '2')), pts(10), CHANNELS)
    assert res.status == 'completed' and not res.incomplete and not res.warnings


def test_real_run_has_no_operation_limit():
    # run_job without max_events (as the real run in core.py) must do all 324000 operations
    from control.jobs.dryrun import DryRunBackend
    clock = SimClock()
    res = run_job(make_job(LONG_JOB), pts(900), DryRunBackend(CHANNELS, clock=clock), experiment_dir=None, clock=clock, save_images=False)
    assert res.status == 'completed' and not res.incomplete, (res.status, res.error)
    assert res.stats['images'] == 120 * 900 * 2


def test_plan_recording_and_figure():
    job = make_job(textwrap.dedent("""
        def run_job(scope):
            for t in scope.timepoints(3, interval=100):
                for p in scope.points('A', 'B'):
                    scope.move_to(p)
                    if p.number == 0:
                        scope.autofocus()
                    scope.capture('488')
                    if t.index == 1:
                        scope.capture('BF')
    """))
    P = pts(4, ids=['A-0', 'A-1', 'B-0', 'B-1'])
    res = dry_run(job, P, CHANNELS, record=True)
    assert res.status == 'completed', res.error
    kinds = [e['kind'] for e in res.events]
    assert kinds.count('timepoint') == 3 and kinds.count('autofocus') == 6
    assert kinds.count('capture') == 12 + 4 and kinds.count('move') == 12
    assert all(e['t1'] >= e['t0'] for e in res.events)
    caps = [e for e in res.events if e['kind'] == 'capture' and e['tp'] == 1]
    assert {e['channel'] for e in caps} == {'Fluorescence 488 nm Ex', 'BF LED matrix full'}
    from control.jobs.plot import plot_job
    fig = plot_job(res, P, channels=list(CHANNELS))
    assert len(fig.axes) >= 5
    # not recorded by default
    assert dry_run(job, P, CHANNELS).events == []


def test_plan_figure_for_late_failed_and_empty_jobs():
    from control.jobs.plot import plot_job, preview_job
    late = make_job('def run_job(scope):\n    for t in scope.timepoints(3, interval=1):\n        for p in scope.points():\n            scope.move_to(p); scope.capture("488")\n')
    P = pts(3)
    res = dry_run(late, P, CHANNELS, record=True)
    assert res.stats['late_timepoints'] >= 1
    assert any(e['kind'] == 'timepoint' and e['late'] > 0.5 for e in res.events)
    plot_job(res, P)
    failed = make_job('def run_job(scope):\n    scope.move_to(scope.points()[0])\n    scope.capture("488")\n    scope.capture("nonexistent")\n')
    res = dry_run(failed, P, CHANNELS, record=True)
    assert res.status == 'failed' and res.events        # what happened until the error is drawn
    plot_job(res, P)
    empty = make_job('def run_job(scope):\n    pass\n')
    fig, res = preview_job(empty, P, CHANNELS, out_path=os.path.join(tempfile.mkdtemp(), 'plan.png'))
    assert res.status == 'completed'
    out = os.path.join(tempfile.mkdtemp(), 'plan.png')
    fig, res = preview_job(late, P, CHANNELS, out_path=out)
    assert os.path.getsize(out) > 10000


def test_live_progress_model():
    from control.jobs_live import JobProgress, fmt_s
    job = make_job(textwrap.dedent("""
        def run_job(scope):
            for t in scope.timepoints(3, interval=100):
                for p in scope.points():
                    scope.move_to(p)
                    scope.capture('488')
                scope.wait(5)
    """))
    P = pts(2)
    plan = dry_run(job, P, CHANNELS, record=True)
    assert plan.status == 'completed', plan.error
    clock = SimClock()
    model = JobProgress(clock=clock.now)
    model.start(plan.events, plan.duration_s, P)
    assert model.n_tp == 3 and model.plan_total_images == 6
    seen = []

    def observer(msg):
        model.update(msg)
        if msg['type'] == 'state':
            seen.append((model.headline()[0], model.tp_boxes(), model.position_boxes(), model.overall_fraction()))
    res = run_job(job, P, TestBackend(clock), clock=clock, save_images=False, observer=observer)
    assert res.status == 'completed' and model.status == 'completed' and model.active is False
    heads = [h for h, *_ in seen]
    assert any(h.startswith('Waiting - time point 2/3 starts in') for h in heads), heads
    assert any(h.startswith('Waiting (scope.wait)') for h in heads)
    assert any(h.startswith('Imaging position #1') for h in heads)
    # while waiting for time point 2 (index 1) box 0 is done, box 1 'waiting', box 2 upcoming
    wait_states = [b for h, b, *_ in seen if h.startswith('Waiting - time point 2/3')]
    assert [st for _, st in wait_states[0]] == ['done', 'waiting', 'upcoming']
    # positions of the current time point: first state of time point 0 has both pending/current
    assert model.images_done == 6 and model.overall_fraction() == 1.0
    assert 'completed' in model.headline()[0]
    assert fmt_s(3725) == '1 h 02 min 05 s' and fmt_s(65) == '1 min 05 s'
    # countdown is extrapolated between messages
    model2 = JobProgress(clock=clock.now)
    model2.start(plan.events, plan.duration_s, P)
    model2.update(dict(type='state', state='waiting', until=100.0, reason='interval', next_tp=1, t=40.0))
    clock.t += 10
    assert abs(model2.headline()[1] - 50.0) < 1e-6


def test_observer_errors_do_not_break_the_job():
    job = make_job('def run_job(scope):\n    scope.move_to(scope.points()[0])\n    scope.capture("488")\n')

    def bad(msg):
        raise RuntimeError('observer bug')
    clock = SimClock()
    res = run_job(job, pts(1), TestBackend(clock), clock=clock, save_images=False, observer=bad)
    assert res.status == 'completed' and res.stats['images'] == 1


def test_job_documentation():
    from control.jobs import job_documentation
    job = make_job(textwrap.dedent("""
        \"\"\"Title line.\"\"\"
        CHANNELS = ['488']
        N = 3
        _private = 1
        def helper(): pass
        def run_job(scope):
            \"\"\"WHAT IT DOES: nothing much.

            YOU PROVIDE: positions.
            \"\"\"
    """))
    t = job_documentation(job)
    assert 'Title line.' in t and 'WHAT IT DOES: nothing much.' in t and 'YOU PROVIDE: positions.' in t
    assert "CHANNELS = ['488']" in t and 'helper' not in t and '_private' not in t
    assert 'no description' in job_documentation(make_job('def run_job(scope):\n    pass\n'))
    assert 'must define' in job_documentation(make_job('X = 1\n')) or 'WARNING' in job_documentation(make_job('X = 1\n'))
    assert 'could not be read' in job_documentation(make_job('def run_job(scope)\n    pass\n'))
    assert 'could not be read' in job_documentation('/nonexistent/job.py')
    # all shipped examples document themselves
    ex = os.path.join(SOFTWARE_DIR, 'job_examples')
    for f in sorted(os.listdir(ex)):
        if f.endswith('.py') and f[0].isdigit() and not f.startswith('01a'):
            txt = job_documentation(os.path.join(ex, f))
            assert 'WHAT IT DOES' in txt and 'YOU PROVIDE' in txt, f


class PausingBackend(TestBackend):
    """Pauses when `trigger` is set (or after N captures) and stays paused for `polls` polls; then offers new positions."""
    def __init__(self, clock, pause_after=None, polls=5, new_positions=None, abort_on_pause=False, **kw):
        super().__init__(clock, **kw)
        self.pause_after, self.polls, self.new_positions, self.abort_on_pause = pause_after, polls, new_positions, abort_on_pause
        self.trigger, self.polls_left, self.fired = False, 0, False
        self.log_ = []
        self._abort = False

    def pause_requested(self):
        if not self.fired and (self.trigger or (self.pause_after is not None and self.n_captures >= self.pause_after)):
            self.fired, self.polls_left = True, self.polls
        if self.polls_left > 0:
            self.polls_left -= 1
            return True
        return False

    def abort_requested(self): return self._abort or super().abort_requested()
    def take_new_positions(self): return self.new_positions
    def on_pause(self):
        self.log_.append('pause')
        if self.abort_on_pause:
            self._abort = True
    def on_resume(self): self.log_.append('resume')


def test_pause_resume_between_operations_and_new_positions():
    job = make_job("""
        def run_job(scope):
            for t in scope.timepoints(2, interval=100):
                for p in scope.points():
                    scope.move_to(p)
                    scope.capture('488')
    """)
    P = pts(2)
    clock = SimClock()
    new = [(10.0, 20.0, 30.0), (1.0, 2.0, 3.0)]            # point 0 edited, point 1 unchanged
    be = PausingBackend(clock, pause_after=1, polls=5, new_positions=new)
    msgs = []
    res = run_job(job, P, be, clock=clock, save_images=False, observer=msgs.append)
    assert res.status == 'completed', res.error
    assert be.log_ == ['pause', 'resume']
    assert abs(res.stats['paused_s'] - 0.4) < 1e-6 and res.stats['images'] == 4
    # time point 2 starts at job time 100 although the clock ran 0.5 s more (paused time is not job time)
    tps = [m for m in msgs if m['type'] == 'timepoint']
    assert abs(tps[1]['start'] - 100.0) < 1e-6 and tps[1]['late'] == 0.0, tps
    assert abs(clock.now() - (res.duration_s + 0.4)) < 1.0
    # the edited position is used later: (10,20,30) appears in the moves of time point 2
    assert (10.0, 20.0, 30.0) in be.moves
    # the stage returned to the current point (first point, the pause came before the 2nd capture of... ) - restore move exists
    states = [m.get('state') for m in msgs if m['type'] == 'state']
    assert 'paused' in states and states.index('paused') < len(states) - 1


def test_pause_during_wait_continues_the_countdown():
    job = make_job("""
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture('488')
            scope.wait(60)
            scope.capture('488')
    """)
    clock = SimClock()
    be = PausingBackend(clock, polls=10)
    msgs = []

    def obs(m):
        msgs.append(m)
        if m['type'] == 'state' and m['state'] == 'waiting' and not be.fired:
            be.trigger = True
    res = run_job(job, pts(1), be, clock=clock, save_images=False, observer=obs)
    assert res.status == 'completed' and res.stats['images'] == 2
    waits = [m for m in msgs if m['type'] == 'state' and m['state'] == 'waiting']
    assert len(waits) == 2 and abs(waits[0]['until'] - waits[1]['until']) < 1e-9      # same target after the pause
    assert abs(res.stats['paused_s'] - 0.9) < 1e-6


def test_pause_with_changed_number_of_positions_is_ignored_and_abort_while_paused():
    job = make_job("""
        def run_job(scope):
            for p in scope.points():
                scope.move_to(p)
                scope.capture('488')
    """)
    clock = SimClock()
    be = PausingBackend(clock, pause_after=1, new_positions=[(0, 0, 0)])        # 1 position instead of 2
    res = run_job(job, pts(2), be, clock=clock, save_images=False)
    assert res.status == 'completed' and any('ignored' in w for w in res.warnings), res.warnings
    clock = SimClock()
    be = PausingBackend(clock, pause_after=1, polls=10 ** 6, abort_on_pause=True)
    res = run_job(job, pts(2), be, clock=clock, save_images=False)
    assert res.status == 'aborted' and be.cleaned


def test_live_model_while_paused_and_countdown_text():
    from control.jobs_live import JobProgress
    clock = SimClock()
    m = JobProgress(clock=clock.now)
    m.start([], None, [])
    m.update(dict(type='state', state='waiting', until=300.0, reason='interval', next_tp=1, t=100.0))
    assert m.countdown_text() == 'Next acquisition in 200 s   (3 min 20 s)'
    clock.t += 0.4
    assert m.countdown_text().startswith('Next acquisition in 200 s') or m.countdown_text().startswith('Next acquisition in 199 s')
    clock.t += 200
    m.update(dict(type='state', state='waiting', until=130.0, reason='interval', next_tp=1, t=100.0))
    m.update(dict(type='state', state='paused', point=0, t=110.0))
    assert m.is_paused() and m.countdown_text() is None and 'PAUSED' in m.headline()[0]
    t1 = m.job_time(); clock.t += 50
    assert m.job_time() == t1 == 110.0                       # job time stands still
    m.update(dict(type='state', state='waiting', until=130.0, reason='interval', next_tp=1, t=110.0))
    assert not m.is_paused() and m.countdown_text() == 'Next acquisition in 20 s'


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('PASS', name)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print('FAIL', name, repr(e))
    sys.exit(1 if fails else 0)
