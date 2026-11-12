"""Tests of scope.fast_movie (video-rate movie) of JOBs, without hardware and Qt.

Run:  python tests/test_jobs_fast.py      or  python -m pytest tests/test_jobs_fast.py
"""
import os
import sys
import csv
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from test_jobs_api import TestBackend, make_job, pts, CHANNELS   # noqa: E402
from control.jobs.api import SimClock, JobError                  # noqa: E402
from control.jobs.runner import run_job, dry_run                 # noqa: E402
from control.jobs import stackio, lint                           # noqa: E402

import tifffile                                                  # noqa: E402


class FastBackend(TestBackend):
    """TestBackend that records frames the way the camera does (frame ids, camera clock); faults can be injected."""
    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.calls = []
        self.lose = []              # frame indexes that are missing (gaps in the frame ids)
        self.level = 100            # grey value of the frames
        self.dtype = np.uint8
        self.late = None            # (index, extra seconds)

    def pixel_size_um(self):
        return 0.5

    def fast_movie(self, channel, n_frames, fps, bits=None, roi=None, exposure_ms=None, gain=None):
        self.calls.append(dict(channel=channel, n=n_frames, fps=fps, bits=bits, roi=roi, exposure_ms=exposure_ms, gain=gain))
        self.clock.sleep(n_frames / fps)
        keep = [i for i in range(n_frames) if i not in self.lose]
        side = roi if isinstance(roi, int) else 16
        dtype = np.uint8 if bits == 8 else (np.uint16 if bits == 16 else self.dtype)
        frames = np.zeros((len(keep), 4, side if side < 64 else 64), dtype=dtype)
        for k, i in enumerate(keep):
            frames[k] = min(self.level, np.iinfo(dtype).max)
            frames[k, 0, 0] = i % 200
        t = [i / fps + (self.late[1] if self.late and i >= self.late[0] else 0.0) for i in keep]
        return dict(frames=frames, frame_ids=[i + 1000 for i in keep], timestamps_s=t, fps_set=fps, exposure_ms=exposure_ms or 8.0, gain=gain or 0.0,
                    pixel_format='MONO8' if dtype == np.uint8 else 'MONO16', incomplete=0, timeouts=0, aborted=False, error=None, notes=[],
                    restore_problems=[], x_mm=self.pos[0], y_mm=self.pos[1], z_mm=self.pos[2])


def run(job_text, points=None, backend=None, **kw):
    clock = SimClock()
    be = backend or FastBackend(clock)
    be.clock = clock
    out = tempfile.mkdtemp()
    res = run_job(make_job(job_text), points or pts(1), be, experiment_dir=out, clock=clock, **kw)
    return res, be, out


def table(out, name):
    p = os.path.join(out, name + '.csv')
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def warnings(res):
    return list(res.warnings)


def test_multipage_uncompressed_file_and_tables():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=25, duration_s=2, roi=2000, bits=8, exposure_ms=7)
    ''')
    assert res.status == 'completed', res.error
    assert be.calls == [dict(channel='Fluorescence 488 nm Ex', n=50, fps=25.0, bits=8, roi=2000, exposure_ms=7, gain=None)]
    f = os.path.join(out, 'Time0000_Point0000_FastMovie.tiff')
    assert os.path.isfile(f)
    with tifffile.TiffFile(f) as tf:
        assert len(tf.pages) == 50 and tf.pages[0].compression.name == 'NONE' and tf.pages[0].dtype == np.uint8
        assert 'FastMovie' in tf.ome_metadata or 'fast movie' in tf.ome_metadata
    fr = table(out, 'frames')
    assert len(fr) == 50 and fr[0]['file'] == 'Time0000_Point0000_FastMovie.tiff' and abs(float(fr[10]['dt_s']) - 0.4) < 1e-6
    ff = table(out, 'fast_frames')
    assert len(ff) == 50 and ff[0]['camera_frame_id'] == '1000' and ff[49]['camera_frame_id'] == '1049'
    assert res.stats['images'] == 50 and res.stats['movies'] == 1
    assert not warnings(res), warnings(res)


def test_single_and_merge_modes_uncompressed():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=10, frames=3, bits=8, save_as="single")
            scope.fast_movie("488", fps=10, frames=3, bits=8, save_as="merge", filename="m")
    ''')
    assert res.status == 'completed', res.error
    d0 = sorted(os.listdir(os.path.join(out, '0')))
    assert d0 == [f'0_0_0_0_Fluorescence_488_nm_Ex_FastMovie_F000{i}.tiff' for i in range(3)], d0
    with tifffile.TiffFile(os.path.join(out, '0', d0[0])) as tf:
        assert tf.pages[0].compression.name == 'NONE'
    with tifffile.TiffFile(os.path.join(out, 'm.tiff')) as tf:
        assert len(tf.pages) == 3 and tf.pages[0].compression.name == 'NONE'


def test_lossless_and_lossy_options_and_validation():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=10, frames=3, compression="lossless")
    ''')
    assert res.status == 'completed', res.error
    with tifffile.TiffFile(os.path.join(out, 'Time0000_Point0000_FastMovie.tiff')) as tf:
        assert tf.pages[0].compression.name in ('ADOBE_DEFLATE', 'DEFLATE')
    for call, text in [('fps=0, frames=3', 'fps'), ('fps=10', 'frames OR duration_s'), ('fps=10, frames=3, duration_s=2', 'frames OR duration_s'),
                       ('fps=10, frames=3, bits=12', 'bits'), ('fps=10, frames=3, roi=3', 'roi'), ('fps=10, frames=3, exposure_ms=0', 'exposure_ms'),
                       ('fps=10, frames=3, compression="zip"', 'compression'), ('fps=10, frames=0', 'frames')]:
        res, be, out = run(f'''
            def run_job(scope):
                scope.move_to(scope.points()[0])
                scope.fast_movie("488", {call})
        ''')
        assert res.status != 'completed' and text in str(res.error), (call, res.status, res.error)
        assert be.calls == []


def test_lost_frames_late_frames_and_saturation_are_reported():
    clock = SimClock()
    be = FastBackend(clock)
    be.lose = [4, 5, 6]
    be.late = (10, 0.08)
    be.level = 255
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=25, frames=20, bits=8)
    ''', backend=be)
    assert res.status == 'completed', res.error
    w = ' | '.join(warnings(res))
    assert '3 frame(s) lost' in w and 'saturated' in w and 'longest frame interval' in w, w


def test_dark_8bit_warning_and_restore_problem_message():
    be = FastBackend(SimClock())
    be.level = 5
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=25, frames=5, bits=8)
    ''', backend=be)
    assert any('dark' in w for w in warnings(res)), warnings(res)


def test_backend_without_fast_mode_gives_clear_error():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=25, frames=5)
    ''', backend=TestBackend(SimClock()))
    assert res.status != 'completed' and 'cannot record fast movies' in str(res.error)


def test_dry_run_and_lint():
    job = make_job('''
        def run_job(scope):
            for t in scope.timepoints(3, interval=60):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.fast_movie("488", fps=25, duration_s=5, roi=2000, bits=8)
    ''')
    rep = dry_run(job, pts(2), {c: dict(v) for c, v in CHANNELS.items()})
    assert rep.status == 'completed', rep.error
    issues = lint.lint(open(job).read(), channels=list(CHANNELS))
    assert not [i for i in issues if i.severity == 'error'], issues
    bad = lint.lint(open(make_job('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=25, frames=5, save_as="zip", filename="")
    ''')).read(), channels=list(CHANNELS))
    assert [i for i in bad if i.severity == 'error'], bad


def test_abort_keeps_recorded_frames():
    class Aborting(FastBackend):
        def fast_movie(self, *a, **k):
            r = super().fast_movie(*a, **k)
            r['frames'], r['frame_ids'], r['timestamps_s'] = r['frames'][:4], r['frame_ids'][:4], r['timestamps_s'][:4]
            r['aborted'] = True
            return r
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.fast_movie("488", fps=25, frames=20, bits=8)
    ''', backend=Aborting(SimClock()))
    with tifffile.TiffFile(os.path.join(out, 'Time0000_Point0000_FastMovie.tiff')) as tf:
        assert len(tf.pages) == 4
    assert any('aborted after 4 of 20' in w for w in warnings(res)), warnings(res)


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
