"""Tests of z-stacks, movies and the lossless/lossy (JPEG2000) saving of JOBs, without hardware and Qt.

Run:  python tests/test_jobs_stacks.py      (needs tifffile; the lossy tests need imagecodecs)
"""
import os
import sys
import csv
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from test_jobs_api import TestBackend, make_job, pts, rows, CHANNELS   # noqa: E402
from control.jobs.api import SimClock, JobError, stack_positions        # noqa: E402
from control.jobs.runner import run_job, dry_run                        # noqa: E402
from control.jobs import stackio                                        # noqa: E402

import tifffile                                                         # noqa: E402

LOSSY_OK = stackio.lossy_available()[0]


class ZBackend(TestBackend):
    """TestBackend whose images encode the stage z (um) and the channel index, and that remembers z moves."""
    def __init__(self, clock, offsets=None, **kw):
        super().__init__(clock, **kw)
        self.z_moves, self.offsets, self.order = [], offsets or {}, []
        self.noise = False

    def move_z(self, z):
        self.clock.sleep(0.2)
        self.pos = (self.pos[0], self.pos[1], z)
        self.z_moves.append(z)

    def channel_z_offset_um(self, name):
        return self.offsets.get(name, 0.0)

    def pixel_size_um(self):
        return 0.5

    def capture(self, channel):
        self.clock.sleep(self.capture_s)
        self.n_captures += 1
        self.order.append((round(self.pos[2] * 1000, 3), channel))
        info = dict(self._channels[channel]); info.update(x_mm=self.pos[0], y_mm=self.pos[1], z_mm=self.pos[2])
        ci = list(self._channels).index(channel)
        if self.noise:
            rng = np.random.default_rng(self.n_captures)
            yy, xx = np.mgrid[0:64, 0:80]
            img = rng.poisson(300 + 3000 * np.exp(-((yy - 30) ** 2 + (xx - 40) ** 2) / 200.0)).astype(np.uint16)
        else:
            img = np.full((8, 10), int(round(self.pos[2] * 1000)) * 10 + ci, dtype=np.uint16)
        return img, info

    def save_image(self, image, path, channel):
        if getattr(self, 'real_tiff', False):
            tifffile.imwrite(path, image)
        else:
            open(path, 'wb').write(b'x')


def run(job_text, points, **kw):
    clock = SimClock()
    bkw = {k: kw.pop(k) for k in list(kw) if k in ('offsets', 'capture_s', 'move_s', 'abort_after')}
    be = ZBackend(clock, **bkw)
    be.real_tiff = bool(kw.pop('real_tiff', False))
    if kw.pop('noise', False):
        be.noise = True
    out = tempfile.mkdtemp()
    res = run_job(make_job(job_text), points, be, experiment_dir=out, clock=clock, **kw)
    return res, be, out


def test_stack_positions_two_of_three():
    assert stack_positions(5, 2.0, None) == (5, 2.0, [-4.0, -2.0, 0.0, 2.0, 4.0])
    assert stack_positions(5, None, 8.0)[:2] == (5, 2.0)
    assert stack_positions(None, 2.0, 8.0)[:2] == (5, 2.0)
    assert stack_positions(5, 2.0, 8.0)[0] == 5                          # all three, consistent
    n, step, offs = stack_positions(4, 1.0, None)                        # even: centre lies between two planes
    assert offs == [-1.5, -0.5, 0.5, 1.5] and abs(sum(offs)) < 1e-12
    assert stack_positions(1)[0] == 1 and stack_positions(1)[2] == [0.0]
    assert stack_positions(None, 0.3, 3.0)[0] == 11                        # float steps
    for bad in [dict(), dict(planes=5), dict(step_um=1.0), dict(span_um=5.0), dict(planes=5, step_um=2.0, span_um=7.0),
                dict(planes=0, step_um=1.0), dict(planes=2.5, step_um=1.0), dict(planes=5, step_um=-1.0),
                dict(planes=5, step_um=0), dict(step_um=3.0, span_um=10.0), dict(planes=3, step_um=5000.0)]:
        try:
            stack_positions(**bad)
        except JobError:
            continue
        raise AssertionError(f'no error for {bad}')


def test_zstack_channel_major_files_and_metadata():
    res, be, out = run('''
        def run_job(scope):
            pt = scope.points()[0]
            scope.move_to(pt)
            d = scope.zstack(["BF", "488"], planes=3, step_um=2.0)
            assert set(d) == {"BF LED matrix full", "Fluorescence 488 nm Ex"} and d["BF LED matrix full"].shape[0] == 3
    ''', pts(1))
    assert res.status == 'completed', res.error
    # centre z is 3.0 mm = 3000 um: planes 2998, 3000, 3002; channel by channel; back at the centre
    assert be.order == [(2998.0, 'BF LED matrix full'), (3000.0, 'BF LED matrix full'), (3002.0, 'BF LED matrix full'),
                        (2998.0, 'Fluorescence 488 nm Ex'), (3000.0, 'Fluorescence 488 nm Ex'), (3002.0, 'Fluorescence 488 nm Ex')], be.order
    assert abs(be.pos[2] - 3.0) < 1e-9
    fr = rows(out, 'frames')
    assert len(fr) == 6 and len({r['file'] for r in fr}) == 1
    f = os.path.join(out, fr[0]['file'])
    assert os.path.basename(f) == 'Time0000_Point0000_Zstack.tiff' and os.path.isfile(f)
    with tifffile.TiffFile(f) as tf:
        assert len(tf.pages) == 6 and tf.is_ome
        a = tf.asarray().reshape(3, 2, 8, 10)                              # Z, C
        assert a[0, 0, 0, 0] == 2998 * 10 + 0 and a[2, 1, 0, 0] == 3002 * 10 + 1
        xml = tf.ome_metadata
    assert 'PositionZ="2998.0"' in xml and 'PositionZ="3002.0"' in xml and 'PhysicalSizeZ="2.0"' in xml and 'PhysicalSizeX="0.5"' in xml
    assert 'Name="Fluorescence 488 nm Ex"' in xml and 'SizeZ="3"' in xml and 'SizeC="2"' in xml


def test_zstack_z_major_order_and_z_moves():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack(["BF", "488"], planes=3, span_um=4.0, order="z_major")
    ''', pts(1))
    assert res.status == 'completed', res.error
    zs = [z for z, c in be.order]
    assert zs == [2998.0, 2998.0, 3000.0, 3000.0, 3002.0, 3002.0], be.order
    assert [c for z, c in be.order][:2] == ['BF LED matrix full', 'Fluorescence 488 nm Ex']
    # fewer z moves than channel_major would need
    assert len(be.z_moves) == 4                                            # 3 planes + back to the centre


def test_zstack_channel_z_offsets_applied():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack(["BF", "488"], planes=2, step_um=2.0)
    ''', pts(1), offsets={'Fluorescence 488 nm Ex': 1.5})
    assert res.status == 'completed', res.error
    assert [z for z, c in be.order] == [2999.0, 3001.0, 3000.5, 3002.5], be.order


def test_zstack_abort_returns_to_centre_and_errors():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack("488", planes=9, step_um=1.0)
    ''', pts(1), abort_after=3)
    assert res.status == 'aborted' and abs(be.pos[2] - 3.0) < 1e-9
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack("488", planes=3)
    ''', pts(1))
    assert res.status == 'failed' and 'two of planes' in res.error, res.error
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack(["488", "488"], planes=3, step_um=1)
    ''', pts(1))
    assert res.status == 'failed' and 'twice' in res.error


def test_zstack_in_timelapse_one_file_per_timepoint_and_point():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=60):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.zstack(["488"], planes=3, step_um=1.0)
    ''', pts(2))
    assert res.status == 'completed', res.error
    files = sorted({r['file'] for r in rows(out, 'frames')})
    assert len(files) == 4 and all(os.path.isfile(os.path.join(out, f)) for f in files), files
    assert files[0].startswith('Time0000_Point0000') and files[-1].startswith('Time0001_Point0001'), files


def test_movie_timing_metadata_and_shape():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            d = scope.movie(["BF", "488"], frames=5, interval_s=2.0)
            assert d["BF LED matrix full"].shape[0] == 5
    ''', pts(1), capture_s=0.3)
    assert res.status == 'completed', res.error
    fr = rows(out, 'frames')
    assert len(fr) == 10
    t = [float(r['time_s']) for r in fr if r['channel'] == 'BF LED matrix full']
    assert np.allclose(np.diff(t), 2.0, atol=1e-6), t                       # exactly on schedule (simulated clock)
    with tifffile.TiffFile(os.path.join(out, fr[0]['file'])) as tf:
        assert len(tf.pages) == 10
        a = tf.asarray().reshape(5, 2, 8, 10)
        xml = tf.ome_metadata
    assert 'SizeT="5"' in xml and 'TimeIncrement="2.0"' in xml and 'achieved interval 2.000 s' in xml
    assert not res.stats['warnings']


def test_movie_duration_back_to_back_and_late_warning():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.movie("488", duration_s=10, interval_s=2.0)
    ''', pts(1))
    assert res.status == 'completed' and len(rows(out, 'frames')) == 5
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.movie(["BF", "488"], frames=4, interval_s=0.5)       # 2 x 0.3 s per round > 0.5 s
    ''', pts(1), capture_s=0.3)
    assert res.status == 'completed'
    assert any('could not keep the requested interval' in w for w in res.stats['warnings']), res.stats['warnings']
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.movie("488", frames=3)
    ''', pts(1), capture_s=0.3)
    t = [float(r['time_s']) for r in rows(out, 'frames')]
    assert res.status == 'completed' and np.allclose(np.diff(t), 0.3)


def test_movie_argument_errors_and_abort():
    for call, msg in [('scope.movie("488")', 'give frames'), ('scope.movie("488", duration_s=5)', 'needs interval_s'),
                      ('scope.movie("488", frames=3, duration_s=5, interval_s=1)', 'not both'),
                      ('scope.movie("488", frames=0)', 'whole number'), ('scope.movie("488", frames=3, interval_s=-1)', 'interval_s'),
                      ('scope.movie([], frames=3)', 'no channel')]:
        res, be, out = run(f'''
            def run_job(scope):
                scope.move_to(scope.points()[0])
                {call}
        ''', pts(1))
        assert res.status == 'failed' and msg in res.error, (call, res.error)
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.movie("488", frames=20, interval_s=1.0)
    ''', pts(1), abort_after=4)
    assert res.status == 'aborted' and be.n_captures == 4 and not rows(out, 'frames')


def test_compression_arguments_are_checked_before_acquiring():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488", compression="zip")
    ''', pts(1))
    assert res.status == 'failed' and 'lossless' in res.error and be.n_captures == 0
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488", quality=80)
    ''', pts(1))
    assert res.status == 'failed' and 'quality and ratio only apply' in res.error and be.n_captures == 0
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack("488", planes=3, step_um=1, compression="lossy", quality=10)
    ''', pts(1))
    assert res.status == 'failed' and 'between' in res.error and be.n_captures == 0


def test_default_single_capture_unchanged():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488")
    ''', pts(1), save_as='single')
    f = rows(out, 'images')[0]['file']
    assert open(os.path.join(out, f), 'rb').read() == b'x'                  # went through backend.save_image as before


def test_lossless_stack_is_bit_identical_and_compressed():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            d = scope.zstack(["488"], planes=3, step_um=1.0)
            scope.log("sum=" + str(int(d["Fluorescence 488 nm Ex"].astype("int64").sum())))
    ''', pts(1), noise=True)
    assert res.status == 'completed', res.error
    f = os.path.join(out, rows(out, 'frames')[0]['file'])
    with tifffile.TiffFile(f) as tf:
        assert tf.pages[0].compression.name == 'ADOBE_DEFLATE'
        got = tf.asarray()
    assert got.shape == (3, 64, 80) or got.shape == (1, 3, 1, 64, 80)
    # re-create the noise images and compare
    rng_imgs = []
    for n in (1, 2, 3):
        rng = np.random.default_rng(n)
        yy, xx = np.mgrid[0:64, 0:80]
        rng_imgs.append(rng.poisson(300 + 3000 * np.exp(-((yy - 30) ** 2 + (xx - 40) ** 2) / 200.0)).astype(np.uint16))
    assert np.array_equal(got.reshape(3, 64, 80), np.stack(rng_imgs))


def test_lossy_stack_movie_and_single_image():
    if not LOSSY_OK:
        print('   (skipped: imagecodecs not available)')
        return
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack("488", planes=3, step_um=1.0, compression="lossy")
            scope.movie("488", frames=3, interval_s=1.0, compression="lossy", quality=90)
            scope.capture("488", compression="lossy")
    ''', pts(1), noise=True)
    assert res.status == 'completed', res.error
    fr = rows(out, 'frames')
    files = sorted({r['file'] for r in fr})
    assert len(files) == 2
    exact = {}
    for n in range(1, 8):
        rng = np.random.default_rng(n)
        yy, xx = np.mgrid[0:64, 0:80]
        exact[n] = rng.poisson(300 + 3000 * np.exp(-((yy - 30) ** 2 + (xx - 40) ** 2) / 200.0)).astype(np.uint16)
    with tifffile.TiffFile(os.path.join(out, files[0])) as tf:
        assert tf.pages[0].compression.name == 'JPEG2000'
        a = tf.asarray().reshape(-1, 64, 80)
    # first file written is the movie or the stack, whichever sorts first; both are close to the originals
    for k in range(len(a)):
        assert np.abs(a[k].astype(int) - exact[1 + k if 'zstack' in files[0] else 4 + k].astype(int)).max() < 80
    one = [r['file'] for r in rows(out, 'images')][0]
    with tifffile.TiffFile(os.path.join(out, one)) as tf:
        assert tf.pages[0].compression.name == 'JPEG2000' and 'lossy JPEG2000' in tf.ome_metadata
    sizes = {f: os.path.getsize(os.path.join(out, f)) for f in files}
    assert all(s > 0 for s in sizes.values())


def test_dry_run_with_stack_and_movie():
    job = make_job('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=120):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.zstack(["BF", "488"], planes=5, step_um=1.0)
                    scope.movie("488", frames=5, interval_s=2.0)
    ''')
    r = dry_run(job, pts(2), CHANNELS)
    assert r.status == 'completed', r.error
    assert r.stats['images'] == 2 * 2 * (10 + 5) and r.stats['stacks'] == 4 and r.stats['movies'] == 4


def test_lint_knows_zstack_and_movie():
    from control.jobs.lint import lint
    ch = list(CHANNELS)
    ok = 'def run_job(scope):\n    """d"""\n    scope.move_to(scope.points()[0])\n    scope.zstack(["488"], planes=3, step_um=1)\n    scope.movie("488", frames=3, interval_s=1)\n'
    assert not [i for i in lint(ok, channels=ch) if i.severity == 'error']
    bad = 'def run_job(scope):\n    scope.move_to(scope.points()[0])\n    scope.zstack(["488", "999"], planes=3)\n    scope.movie("488", interval_s=1, compression="zip")\n'
    msgs = ' | '.join(i.message for i in lint(bad, channels=ch) if i.severity == 'error')
    assert 'two of planes' in msgs and "unknown channel '999'" in msgs and 'frames=' in msgs and 'lossless' in msgs, msgs


def test_lossy_defaults_to_60_db_and_ratio_is_an_option():
    if not LOSSY_OK:
        print('   (skipped: imagecodecs not available)')
        return
    rng = np.random.default_rng(5)
    H, W = 1200, 1500
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    sig = np.zeros((H, W), np.float32)
    for _ in range(80):
        cy, cx, s, a = rng.integers(0, H), rng.integers(0, W), rng.uniform(3, 9), rng.uniform(400, 3000)
        sig += a * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * s * s))
    img = rng.poisson(sig + 100).astype(np.uint16)
    data = np.stack([img, np.roll(img, 7, axis=1)])[None, None]           # (T=1, Z=1, C=2, Y, X)
    d = tempfile.mkdtemp()
    FL = ['Fluorescence 488 nm Ex', 'BF LED matrix full']
    info = stackio.write_ome_tiff(os.path.join(d, 'a.ome.tiff'), data, FL, compression='lossy')           # default: error = 1x noise, per channel
    assert info['targets'] is None and len(info['qualities']) == 2 and info['noise_factor'] == 1.0, info
    assert all(c['rmse'] is None for c in info['channels']), info          # single image: formula only, no verification encode
    back = tifffile.imread(info['file']).reshape(2, H, W)
    assert np.abs(back[0].astype(float) - data[0, 0, 0]).max() < 2000 and info['ratio'] > 2
    # stack: the formula is verified, the error ends up near 1x noise
    two = np.concatenate([data, data], axis=1)                                                              # Z=2
    infoz = stackio.write_ome_tiff(os.path.join(d, 'z.ome.tiff'), two, FL, compression='lossy')
    for c in infoz['channels']:
        assert c['rmse'] is not None and 0.5 < c['rmse'] / c['noise'] < 2.0, infoz
    # noise estimate: white noise sigma 20 -> ~20, also with a smooth structure added; row/column subsampling
    wn = (rng.normal(0, 20, (512, 512)) + 1000 + 300 * np.sin(np.arange(512) / 30.0)[None, :]).astype(np.float32).clip(0, 65535).astype(np.uint16)
    assert 17 < stackio.estimate_noise(wn) < 23, stackio.estimate_noise(wn)
    # ratio mode: calibrated per channel (a number for all channels, or a dict; channels not in the dict use the noise-relative default)
    info2 = stackio.write_ome_tiff(os.path.join(d, 'b.ome.tiff'), data, FL, compression='lossy', ratio=100)
    assert info2['targets'] == [100.0, 100.0] and 60 < info2['ratio'] and len(info2['channels']) == 2, info2
    info3 = stackio.write_ome_tiff(os.path.join(d, 'c.ome.tiff'), data, FL, compression='lossy', ratio=20)
    assert info3['ratio'] < info2['ratio'] and info3['quality_db'] > info2['quality_db']
    info4 = stackio.write_ome_tiff(os.path.join(d, 'd.ome.tiff'), data, FL, compression='lossy', ratio={FL[0]: 30})
    assert info4['targets'] == [30.0, None] and info4['channels'][1]['noise'] is not None and info4['qualities'][0] != info4['qualities'][1]
    info5 = stackio.write_ome_tiff(os.path.join(d, 'e.ome.tiff'), data, FL, compression='lossy', quality=90)
    assert info5['qualities'] == [90.0, 90.0] and info5['targets'] is None
    for kw in (dict(compression='lossy', quality=80, ratio=100), dict(compression='lossless', ratio=100), dict(compression='lossy', ratio=1)):
        try:
            stackio.normalize_compression(kw['compression'], kw.get('quality'), kw.get('ratio'))
        except stackio.StackIOError:
            continue
        raise AssertionError(kw)
    try:
        stackio.write_ome_tiff(os.path.join(d, 'f.ome.tiff'), data, FL, compression='lossy', ratio={'nope': 30})
    except stackio.StackIOError:
        pass
    else:
        raise AssertionError('unknown channel in ratio dict accepted')


def test_job_logs_lossy_report():
    if not LOSSY_OK:
        return
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack("488", planes=3, step_um=1.0, compression="lossy")
    ''', pts(1), noise=True)
    assert res.status == 'completed', res.error
    r = rows(out, 'lossy_files')
    assert len(r) == 1 and float(r[0]['achieved_ratio']) > 1 and r[0]['target_ratio'] == '' and r[0]['quality_db'] != '', r
    log = open(os.path.join(out, 'job_log.txt'), encoding='utf-8').read()
    assert 'lossy JPEG2000' in log and 'smaller than raw' in log


def _names(out, col='file', table='images'):
    return [r[col].replace('\\', '/') for r in rows(out, table)]


def test_multipage_capture_channels_one_file_per_point_and_timepoint():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=60):
                for pt in scope.points():
                    scope.move_to(pt)
                    scope.capture_channels(["BF", "488"])
    ''', pts(2))
    assert res.status == 'completed', res.error
    n = _names(out)
    assert len(n) == 8 and sorted(set(n)) == ['Time0000_Point0000.tiff', 'Time0000_Point0001.tiff', 'Time0001_Point0000.tiff', 'Time0001_Point0001.tiff'], n
    with tifffile.TiffFile(os.path.join(out, n[0])) as tf:
        assert len(tf.pages) == 2 and tf.is_ome and 'BF LED matrix full' in tf.ome_metadata and 'Fluorescence 488 nm Ex' in tf.ome_metadata
    assert not [f for f in os.listdir(out) if os.path.isdir(os.path.join(out, f))], 'multipage files are written flat into the experiment folder'


def test_single_files_follow_flexible_multipoint_layout_and_ver_counter():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=60):
                scope.move_to(scope.points()[0])
                scope.capture("488")
                scope.capture("488")
    ''', pts(1), save_as='single')
    assert res.status == 'completed', res.error
    n = _names(out)
    assert n == ['0/0_0_0_0_Fluorescence_488_nm_Ex.tiff', '0/0_0_0_0_Fluorescence_488_nm_Ex_ver_2.tiff',
                 '1/0_0_0_0_Fluorescence_488_nm_Ex.tiff', '1/0_0_0_0_Fluorescence_488_nm_Ex_ver_2.tiff'], n
    assert all(os.path.isfile(os.path.join(out, f)) for f in n)


def test_same_channel_twice_in_multipage_gets_ver_2():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488")
            scope.capture("BF")
            scope.capture("488")
    ''', pts(1))
    assert _names(out) == ['Time0000_Point0000.tiff', 'Time0000_Point0000.tiff', 'Time0000_Point0000_ver_2.tiff'], _names(out)
    assert all(os.path.isfile(os.path.join(out, f)) for f in set(_names(out)))


def test_zstack_and_movie_names_multipage_and_single():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.zstack(["488", "BF"], planes=2, step_um=1.0)
            scope.movie("488", frames=2, interval_s=1.0)
            scope.zstack(["488"], planes=2, step_um=1.0, save_as="single")
            scope.movie(["488"], frames=2, interval_s=1.0, save_as="single")
    ''', pts(1))
    assert res.status == 'completed', res.error
    n = sorted(set(_names(out, table='frames')))
    assert n == ['0/0_0_0_0_Fluorescence_488_nm_Ex.tiff', '0/0_0_0_0_Fluorescence_488_nm_Ex_Movie_F0000.tiff',
                 '0/0_0_0_0_Fluorescence_488_nm_Ex_Movie_F0001.tiff', '0/0_0_0_1_Fluorescence_488_nm_Ex.tiff',
                 'Time0000_Point0000_Movie.tiff', 'Time0000_Point0000_Zstack.tiff'], n
    assert all(os.path.isfile(os.path.join(out, f)) for f in n)


def test_filename_argument():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488", filename="my_first.ome.tiff")
            scope.capture("488", filename="my_first")
            scope.capture_channels(["BF", "488"], filename="both")
            scope.capture_channels(["BF", "488"], filename="split", save_as="single")
            scope.zstack("488", planes=2, step_um=1.0, filename="stack", save_as="single")
            scope.movie("488", frames=2, filename="mov")
    ''', pts(1))
    assert res.status == 'completed', res.error
    n = _names(out) + _names(out, table='frames')
    assert n[0] == 'my_first.tiff' and n[1] == 'my_first_ver_2.tiff' and n[2] == n[3] == 'both.tiff', n
    assert n[4:6] == ['0/split_BF_LED_matrix_full.tiff', '0/split_Fluorescence_488_nm_Ex.tiff'], n
    assert n[6:8] == ['0/stack_Z0000_Fluorescence_488_nm_Ex.tiff', '0/stack_Z0001_Fluorescence_488_nm_Ex.tiff'] and n[8:] == ['mov.tiff'] * 2, n
    assert all(os.path.isfile(os.path.join(out, f)) for f in set(n))
    bad, _, _ = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture("488", filename="")
    ''', pts(1))
    assert bad.status == 'failed' and 'filename' in bad.error


def test_multipage_lossy_capture_and_lint_of_naming_args():
    from control.jobs.lint import lint
    ch = list(CHANNELS)
    bad = 'def run_job(scope):\n    scope.move_to(scope.points()[0])\n    scope.capture("488", filename="")\n    scope.capture("488", save_as="yes")\n'
    msgs = ' | '.join(i.message for i in lint(bad, channels=ch) if i.severity == 'error')
    assert 'filename must be' in msgs and 'save_as must be' in msgs, msgs
    if LOSSY_OK:
        res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture_channels(["BF", "488"], compression="lossy")
    ''', pts(1), noise=True)
        assert res.status == 'completed', res.error
        f = os.path.join(out, 'Time0000_Point0000.tiff')
        with tifffile.TiffFile(f) as tf:
            assert len(tf.pages) == 2 and tf.pages[0].compression.name.startswith('JPEG2000')


def test_merge_mode_capture_channels_zstack_movie():
    res, be, out = run('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=60):
                scope.move_to(scope.points()[0])
                scope.capture_channels(["BF", "488"])
                scope.zstack(["488", "BF"], planes=2, step_um=1.0)
                scope.movie("488", frames=2, interval_s=1.0)
    ''', pts(1), real_tiff=True, save_as='merge')
    assert res.status == 'completed', res.error
    names = sorted(set(_names(out) + _names(out, table='frames')))
    assert names == ['Time0000_Point0000.tiff', 'Time0000_Point0000_Movie.tiff', 'Time0000_Point0000_Zstack.tiff',
                     'Time0001_Point0000.tiff', 'Time0001_Point0000_Movie.tiff', 'Time0001_Point0000_Zstack.tiff'], names
    for n in names:
        with tifffile.TiffFile(os.path.join(out, n)) as tf:
            exp = {'Time0000_Point0000.tiff': 2, 'Time0001_Point0000.tiff': 2}.get(n, 4 if 'Zstack' in n else 2)
            assert len(tf.pages) == exp and tf.is_ome, (n, len(tf.pages))
    left = [f for d, _, fs in os.walk(out) for f in fs if f.endswith('.tiff') and not f.startswith('Time')]
    assert not left, f'single files must be deleted after the merge: {left}'
    assert not [f for f in os.listdir(out) if f.endswith('.part')]
    # content identical to the directly written multipage files
    res2, be2, out2 = run('''
        def run_job(scope):
            for t in scope.timepoints(2, interval=60):
                scope.move_to(scope.points()[0])
                scope.capture_channels(["BF", "488"])
                scope.zstack(["488", "BF"], planes=2, step_um=1.0)
                scope.movie("488", frames=2, interval_s=1.0)
    ''', pts(1), real_tiff=True, save_as='multipage')
    assert res2.status == 'completed', res2.error
    for n in names:
        assert np.array_equal(tifffile.imread(os.path.join(out, n)), tifffile.imread(os.path.join(out2, n))), n


def test_merge_mode_lossy_and_kept_when_a_single_file_is_missing():
    res, be, out = run('''
        def run_job(scope):
            scope.move_to(scope.points()[0])
            scope.capture_channels(["BF", "488"], compression="lossy")
    ''', pts(1), real_tiff=True, save_as='merge', noise=True)
    assert res.status == 'completed', res.error
    if LOSSY_OK:
        with tifffile.TiffFile(os.path.join(out, 'Time0000_Point0000.tiff')) as tf:
            assert len(tf.pages) == 2 and tf.pages[0].compression.name.startswith('JPEG2000')
    # a missing single file: the merged file is written with a zero page, the existing single files stay, the error is logged
    import control.jobs.stackio as sio
    d = tempfile.mkdtemp()
    a = np.full((8, 10), 7, np.uint16)
    tifffile.imwrite(os.path.join(d, 'p0.tiff'), a)
    src = sio.PageFiles({(0, 0, 0): os.path.join(d, 'p0.tiff'), (0, 0, 1): os.path.join(d, 'gone.tiff')}, (1, 1, 2), np.uint16, (8, 10))
    sio.write_ome_tiff(os.path.join(d, 'm.tiff'), src, ['A', 'B'])
    assert src.missing == [(0, 0, 1)]
    back = tifffile.imread(os.path.join(d, 'm.tiff')).reshape(2, 8, 10)
    assert back[0].min() == 7 and back[1].max() == 0 and os.path.isfile(os.path.join(d, 'p0.tiff'))


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
