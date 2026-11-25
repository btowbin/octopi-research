"""The camera ROI must always be centred, and the software crops must always equal the hardware ROI.

Real control/camera_flir.py and control/camera_roi_widget.py against the fake PySpin (tests/fake_pyspin.py), with the
LIMITS OF A REAL SPINNAKER CAMERA added: Width/Height maximum = sensor - current offset, OffsetX/OffsetY maximum = sensor - size,
misaligned values and any ROI write while streaming raise. No Qt and no camera needed (the widget methods are bound to a
plain object).
Run:  python tests/test_roi_centring.py      (or python -m pytest tests/test_roi_centring.py)
"""
import os
import random
import sys
import types

SW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SW)
sys.path.insert(0, os.path.join(SW, 'tests'))
import fake_pyspin  # noqa: E402
sys.modules['PySpin'] = fake_pyspin
try:
    import qtpy.QtWidgets  # noqa: F401
except ImportError:             # no Qt installed: the widget class is only imported, never instantiated
    class _Base:
        def __init__(self, *a, **k):
            pass
    for _name in ('qtpy', 'qtpy.QtWidgets'):
        _m = types.ModuleType(_name)
        _m.__getattr__ = lambda n: type(n, (_Base,), {'Panel': 0, 'Raised': 0})
        sys.modules[_name] = _m
import control.camera_flir as cf  # noqa: E402
from control.camera_roi_widget import CameraROIWidget, CENTRE_TOLERANCE_PX  # noqa: E402

SENSOR_W, SENSOR_H = fake_pyspin.SENSOR_W, fake_pyspin.SENSOR_H


def new_camera(inc_ox=16, inc_oy=2, inc_h=2):
    """FLIR camera on the fake SDK with Spinnaker's dynamic ROI limits."""
    fake = fake_pyspin.reset()
    nw, nh, nx, ny = fake.n_width, fake.n_height, fake.n_offx, fake.n_offy
    nw.inc, nh.inc, nx.inc, ny.inc = 16, inc_h, inc_ox, inc_oy
    nw.vmax = lambda: (SENSOR_W - nx.value) // nw.inc * nw.inc
    nh.vmax = lambda: (SENSOR_H - ny.value) // nh.inc * nh.inc
    nx.vmax = lambda: (SENSOR_W - nw.value) // nx.inc * nx.inc
    ny.vmax = lambda: (SENSOR_H - nh.value) // ny.inc * ny.inc
    fake.fail_next = None                  # name of a node whose next write raises (failure injection)
    for node in (nw, nh, nx, ny):
        orig = node.SetValue

        def setv(v, *a, node=node, orig=orig):
            if fake.streaming:
                raise fake_pyspin.SpinnakerException('cannot change the ROI while streaming', -2006)
            if fake.fail_next == node.name:
                fake.fail_next = None
                raise fake_pyspin.SpinnakerException(f'{node.name}: injected failure', -2002)
            if (v - (node.vmin or 0)) % node.inc:
                raise fake_pyspin.SpinnakerException(f'{node.name}: {v} is not a multiple of {node.inc}', -2002)
            orig(v, *a)
        node.SetValue = setv
        node.on_change = None
    cam = cf.Camera(sn=None)
    cam.open()
    cam.set_pixel_format('MONO16')
    cam.set_software_triggered_acquisition()
    cam.start_streaming()
    return cam, fake


class Spin:
    def __init__(self, v):
        self.v = v

    def value(self):
        return self.v

    def setValue(self, v):
        self.v = v

    def blockSignals(self, b):
        pass


class Label:
    def __init__(self):
        self.text, self.style = '', ''

    def setText(self, t):
        self.text = t

    def setStyleSheet(self, s):
        self.style = s


class Handler:
    """Records the software crops."""
    def __init__(self):
        self.crop = None

    def set_crop(self, w, h):
        self.crop = (w, h)


def new_widget(cam):
    """The widget's methods bound to a plain object (no Qt needed)."""
    w = types.SimpleNamespace(camera=cam, spin_width=Spin(0), spin_height=Spin(0), label_actual=Label(),
                              stream_handler=Handler(), autofocus_controller=Handler(),
                              multipoint_controller=types.SimpleNamespace(crop_width=None, crop_height=None))
    for name in ('update_roi', '_write_roi', 'check_roi', '_read_hw_roi', '_sensor_size', '_apply_software_crops', '_show_status',
                 'centred_offset', 'valid_size'):
        f = CameraROIWidget.__dict__[name]
        setattr(w, name, f.__func__ if isinstance(f, staticmethod) else types.MethodType(f, w))
    return w


def hw(fake):
    return fake.n_offx.value, fake.n_offy.value, fake.n_width.value, fake.n_height.value


def centre_error(fake):
    ox, oy, w, h = hw(fake)
    return abs(ox - (SENSOR_W - w) / 2), abs(oy - (SENSOR_H - h) / 2)


def assert_centred_and_crops_equal(w, fake, what=''):
    dx, dy = centre_error(fake)
    assert dx <= CENTRE_TOLERANCE_PX and dy <= CENTRE_TOLERANCE_PX, f'{what}: ROI off centre by {dx}, {dy} px: {hw(fake)}'
    _, _, wi, he = hw(fake)
    assert w.stream_handler.crop == (wi, he) and w.autofocus_controller.crop == (wi, he), (what, w.stream_handler.crop, hw(fake))
    assert (w.multipoint_controller.crop_width, w.multipoint_controller.crop_height) == (wi, he), what


def test_many_roi_changes_stay_centred():
    random.seed(1)
    for inc_ox, inc_oy, inc_h in ((4, 2, 2), (16, 2, 2), (16, 8, 8)):
        cam, fake = new_camera(inc_ox, inc_oy, inc_h)
        w = new_widget(cam)
        for i in range(60):
            w.spin_width.v = random.choice([16, 800, 1000, 1500, 2000, 2096, 2104, SENSOR_W, random.randrange(16, SENSOR_W + 1)])
            w.spin_height.v = random.choice([16, 800, 1000, 1500, 2000, 2048, SENSOR_H, random.randrange(16, SENSOR_H + 1)])
            w.update_roi()
            assert_centred_and_crops_equal(w, fake, f'inc {inc_ox}/{inc_oy}/{inc_h} change {i}')


def test_failed_write_is_repaired():
    """The three writes are not atomic: if the size write fails the offsets are already 0 (top-left corner)."""
    cam, fake = new_camera()
    w = new_widget(cam)
    w.spin_width.v, w.spin_height.v = 2000, 1500
    w.update_roi()
    assert_centred_and_crops_equal(w, fake, 'start')
    start = hw(fake)
    fake.fail_next = 'Height'
    w.spin_width.v, w.spin_height.v = 1000, 1000
    w.update_roi()                                     # must not raise
    assert hw(fake)[3] == start[3], 'the failed height write left the old height'     # half-applied ROI (width 1000, old height)
    assert_centred_and_crops_equal(w, fake, 'after the failed write')
    assert 'FAILED' in w.label_actual.text and 'red' in w.label_actual.style, w.label_actual.text
    assert (w.spin_width.value(), w.spin_height.value()) == hw(fake)[2:], 'spin boxes show the real size'


def test_any_other_drift_is_repaired_before_an_acquisition():
    """Whatever moves the offsets (a failed restore, another tool, an unknown cause): check_roi() centres it again."""
    cam, fake = new_camera()
    w = new_widget(cam)
    w.spin_width.v, w.spin_height.v = 2096, 2048
    w.update_roi()
    assert_centred_and_crops_equal(w, fake, 'start')
    cam.set_ROI(offset_x=0, offset_y=0)                # top-left corner: what a live view would show shifted
    assert centre_error(fake)[0] > 100
    assert w.check_roi(fix=False) is False              # only reports
    assert centre_error(fake)[0] > 100 and 'NOT CENTRED' in w.label_actual.text
    assert w.check_roi() is True                        # repairs
    assert_centred_and_crops_equal(w, fake, 'after repair')
    assert 'centred again' in w.label_actual.text
    # size changed behind the widget's back: crops follow the real size
    cam.set_ROI(offset_x=0, offset_y=0)
    cam.set_ROI(width=1024, height=1024)
    w.check_roi()
    assert_centred_and_crops_equal(w, fake, 'size drift')
    assert w.spin_width.value() == 1024


def test_fast_movie_leaves_the_roi_centred():
    cam, fake = new_camera()
    w = new_widget(cam)
    w.spin_width.v, w.spin_height.v = 2096, 2048
    w.update_roi()
    before = hw(fake)
    out = cam.fast_acquire(5, fps=25, roi=1000, bits=8, exposure_ms=5)
    ox, oy, ww, hh = out['roi']                          # ROI during the movie
    assert abs(ox - (SENSOR_W - ww) / 2) <= CENTRE_TOLERANCE_PX and abs(oy - (SENSOR_H - hh) / 2) <= CENTRE_TOLERANCE_PX, out['roi']
    assert hw(fake) == before and not out['restore_problems']
    assert w.check_roi(fix=False) is True


def test_camera_that_cannot_report_its_roi_works_as_before():
    class Silent:                                        # e.g. simulation / Toupcam: no get_ROI
        WidthMax, HeightMax, Width, Height = SENSOR_W, SENSOR_H, SENSOR_W, SENSOR_H

        def __init__(self):
            self.calls = []

        def set_ROI(self, **kw):
            self.calls.append(kw)
    cam = Silent()
    w = new_widget(cam)
    w.spin_width.v, w.spin_height.v = 1000, 800
    w.update_roi()
    assert cam.calls[0] == dict(offset_x=0, offset_y=0) and cam.calls[1] == dict(width=1000, height=800)
    assert cam.calls[2] == dict(offset_x=(SENSOR_W - 1000) // 2 // 8 * 8, offset_y=(SENSOR_H - 800) // 2 // 8 * 8)
    assert w.stream_handler.crop == (1000, 800) and w.check_roi() is True


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('PASS', name)
            except Exception as e:
                failures += 1
                import traceback
                traceback.print_exc()
                print('FAIL', name, e)
    sys.exit(1 if failures else 0)
