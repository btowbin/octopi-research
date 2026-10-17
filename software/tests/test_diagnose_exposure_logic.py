"""Tests the analysis logic of diagnose_exposure.py with a FAKE camera (no hardware, no Qt).

The fake camera can behave in three known ways; the diagnostic must recognise each:
  none  : the set exposure is used for the next frame (correct camera)
  lag1  : a new exposure only takes effect one frame later (frame-latched exposure)  -> the reported bug
  time  : a new exposure takes effect 30 ms after it was set (timing problem)

Run:  python tests/test_diagnose_exposure_logic.py      (or: python -m pytest tests)
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diagnose_exposure as dx

CHANNEL_RATE = {'BF': 30.0, 'GFP': 12.0, 'RFP': 6.0}     # counts per ms
DARK = 100.0
CONFIG_EXPOSURE = {'BF': 5.0, 'GFP': 5.0, 'RFP': 5.0}     # overwritten by the test


class FakeRig:
    def __init__(self, behaviour, noise=0.5, seed=1, saturate_at=None):
        self.behaviour = behaviour
        self.rng = np.random.default_rng(seed)
        self.noise = noise
        self.t = 0.0                    # fake clock (s)
        self.cfg = dict(CONFIG_EXPOSURE)
        self.channel = 'BF'
        self.set_value = 5.0
        self.t_set = -10.0
        self.value_before = 5.0
        self.latch = 5.0
        self.saturate_at = saturate_at
        self.n = 0

    # --- rig interface
    def channels(self): return list(self.cfg)
    def get_config_exposure(self, n): return self.cfg[n]
    def set_config_exposure(self, n, ms): self.cfg[n] = ms
    def info(self): return {}
    def sleep(self, s): self.t += s
    def readback_exposure_ms(self): return self.set_value

    def _effective(self):
        return self.set_value if self.t - self.t_set >= 0.03 else self.value_before

    def set_camera_exposure(self, ms):
        self.value_before = self._effective()
        self.set_value = ms
        self.t_set = self.t

    def switch_channel(self, name):
        self.channel = name
        self.set_camera_exposure(self.cfg[name])
        self.t += 0.01

    def _applied(self):
        if self.behaviour == 'none':
            return self.set_value
        if self.behaviour == 'lag1':
            a = self.latch
            self.latch = self.set_value
            return a
        if self.behaviour == 'time':
            return self._effective()
        raise ValueError(self.behaviour)

    def grab(self, illum=True):
        e = self._applied()
        f = dx.Frame()
        f.t_trigger = self.t
        self.t += e / 1000.0 + 0.05
        f.t_done = self.t
        signal = CHANNEL_RATE[self.channel] * e if illum else 0.0
        f.mean = DARK + signal + self.rng.normal(0, self.noise)
        f.frame_id = self.n
        self.n += 1
        f.readback_ms = self.set_value
        f.saturated = bool(self.saturate_at and f.mean > self.saturate_at)
        return f


def run(behaviour, exposures=(10.0, 40.0, 160.0), delays=(0.0, 0.2, 1.0), rounds=4):
    rig = FakeRig(behaviour)
    names = ['BF', 'GFP', 'RFP']
    exp = dict(zip(names, exposures))
    for n in names:
        rig.set_config_exposure(n, exp[n])
    rows, warnings = [], []
    res = dx.run_experiments(rig, names, exp, list(delays), rounds, warnings, rows)
    hints = dx.interpret(res, rows, list(delays))
    return res, rows, warnings, hints, list(delays)


def test_correct_camera():
    res, rows, warnings, hints, delays = run('none')
    assert not warnings, warnings
    for d in delays:
        assert dx.count(rows, exp='E2', delay_s=d)['frac_cur'] == 1.0
        assert dx.count(rows, exp='E3', delay_s=d, j=0)['frac_cur'] == 1.0
    assert dx.count(rows, exp='E3b')['frac_cur'] == 1.0
    assert any('camera and driver are fine' in h for h in hints), hints
    assert any('NOT reproduced' in h for h in hints), hints


def test_one_frame_lag_is_the_reported_bug():
    res, rows, warnings, hints, delays = run('lag1')
    assert not warnings, warnings
    for d in delays:      # waiting does not help
        assert dx.count(rows, exp='E2', delay_s=d)['frac_prev'] == 1.0
        assert dx.count(rows, exp='E3', delay_s=d, j=0)['frac_prev'] == 1.0
        assert dx.count(rows, exp='E3', delay_s=d, j=1)['frac_cur'] == 1.0
    # one discarded frame after the switch fixes it
    assert dx.count(rows, exp='E3b')['frac_cur'] == 1.0
    assert any('independent of waiting time' in h for h in hints), hints
    assert any('lag = 1 frame' in h for h in hints), hints


def test_timing_lag():
    res, rows, warnings, hints, delays = run('time')
    assert not warnings, warnings
    assert dx.count(rows, exp='E2', delay_s=0.0)['frac_prev'] == 1.0
    assert dx.count(rows, exp='E2', delay_s=0.2)['frac_cur'] == 1.0
    assert dx.count(rows, exp='E3', delay_s=1.0, j=0)['frac_cur'] == 1.0
    assert any('timing problem' in h for h in hints), hints


def test_saturation_is_reported_not_misread():
    rig = FakeRig('none', saturate_at=3000)
    names = ['BF', 'GFP', 'RFP']
    exp = dict(zip(names, (10.0, 40.0, 160.0)))
    for n in names:
        rig.set_config_exposure(n, exp[n])
    rows, warnings = [], []
    dx.run_experiments(rig, names, exp, [0.0], 2, warnings, rows)
    assert any('saturated' in w for w in warnings), warnings


def test_report_renders():
    import tempfile
    res, rows, warnings, hints, delays = run('lag1', rounds=2)
    out = tempfile.mkdtemp()

    class A:  # unused by build_report except via sys.argv
        pass
    text = dx.write_outputs(out, A(), {'camera': 'fake'}, res, rows, [], delays, warnings, 1.0)
    assert 'AUTOMATIC INTERPRETATION' in text and 'PREVIOUS' in text
    assert os.path.isfile(os.path.join(out, 'frames.csv')) and os.path.isfile(os.path.join(out, 'summary.json'))


def test_analyze_sequence_for_real_acquisition():
    exposures = {'BF': 10.0, 'GFP': 40.0, 'RFP': 160.0}
    calib = {c: dict(k=CHANNEL_RATE[c], dark=DARK) for c in exposures}
    order = ['BF', 'GFP', 'RFP'] * 2
    # frames with the exposure of the previous channel (the bug), first frame with the exposure of the last channel
    seq = []
    for i, c in enumerate(order):
        e_applied = exposures[order[i - 1]] if i > 0 else exposures['RFP']
        seq.append((c, DARK + CHANNEL_RATE[c] * e_applied, {}))
    rows = dx.analyze_sequence(seq, exposures, calib, first_prev_exposure=exposures['RFP'])
    assert all(r['verdict'] == 'PREVIOUS' for r in rows), [r['verdict'] for r in rows]
    seq = [(c, DARK + CHANNEL_RATE[c] * exposures[c], {}) for c in order]
    rows = dx.analyze_sequence(seq, exposures, calib, first_prev_exposure=exposures['RFP'])
    assert all(r['verdict'] == 'current' for r in rows)


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('PASS', name)
            except Exception as e:
                fails += 1
                import traceback
                traceback.print_exc()
                print('FAIL', name, repr(e))
    sys.exit(1 if fails else 0)
