"""Diagnose the 'exposure time of the previous channel' problem (camera exposure lags one channel behind).

Symptom: with 2+ channels with DIFFERENT exposure times, channel 2 is imaged with the exposure of channel 1,
channel 3 with that of channel 2, channel 1 with that of the last channel.

What this script does: it measures the EFFECTIVE exposure of every frame from the image brightness
(brightness is proportional to exposure for a constant sample and illumination) and compares it with the
exposure that was requested for that frame and with the exposure of the channel before. It runs the same
call sequence as the acquisition worker (signal -> set_microscope_mode -> turn on illumination ->
send_trigger -> read_frame -> turn off illumination) in a worker thread, in steps that separate the
possible origins:

  E1  register   camera.set_exposure_time(x) -> does the camera register read back x? (immediately / after 50 ms)
  E2  direct     ONE channel, exposure alternates A,B,A,B with 0 / 0.2 / 1 s wait between set and trigger.
                 Camera/driver level only, no channel switching, no Qt signals.
  E3  switch     channels cycled through the real signal path (BlockingQueued -> set_microscope_mode ->
                 widgets -> camera); the first 3 frames after each switch are measured, for several waits.
  E3b workaround one discarded dark frame after the switch (the old fix) -> does the next frame have the right
                 exposure, and what does it cost in time?
  E5  real       a real acquisition with the MultiPoint worker (1 position, Nt time points); the saved
                 images are analysed the same way, and every camera call is logged with timestamps.

It does NOT move the stage. It switches the illumination on/off many times (short flashes). Use a static,
in-focus sample with moderate signal (not saturated, clearly above dark).  Exposures are changed in memory
only; channel_configurations.xml is backed up and restored.

Usage (from anywhere; the script changes to the software folder itself):
    python tests\\diagnose_exposure.py --hardware --out-dir D:\\exposure_diag --channels "BF LED matrix full,Fluorescence 488 nm Ex,Fluorescence 561 nm Ex"
    python tests\\diagnose_exposure.py --simulation --out-dir C:\\temp\\exposure_diag     (plumbing check only)
Options: --exposures 10,40,160 (ms per channel in the given order; 'keep' = use the configured values),
         --rounds 6, --delays 0,0.2,1.0, --skip-real, --yes.
Result: <out-dir>\\exposure_diag_<time>\\report.txt (send this) + frames.csv + events.csv + summary.json
"""
import os
import sys
import csv
import json
import time
import argparse
import datetime
import traceback
import platform
import shutil
import glob
import subprocess
import threading
from dataclasses import dataclass

import numpy as np

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = time.perf_counter()
TOL = 0.25          # |implied/expected - 1| below this counts as 'matches'
MIN_SIGNAL_COUNTS = 30


def log(msg=''):
    print(msg, flush=True)


# ------------------------------------------------------------------------------------------------
# data + helpers (no hardware, no Qt)
# ------------------------------------------------------------------------------------------------
class ExperimentError(Exception):
    pass


@dataclass
class Frame:
    ok: bool = True
    mean: float = float('nan')
    p999: float = float('nan')
    saturated: bool = False
    t_trigger: float = 0.0     # perf_counter at send_trigger
    t_done: float = 0.0        # perf_counter when read_frame returned
    frame_id: object = None
    readback_ms: object = None
    err: str = ''


def verdict(implied, e_cur, e_prev, tol=TOL):
    """Which requested exposure does the measured (implied) exposure match?"""
    if implied is None or not np.isfinite(implied):
        return 'invalid'
    near_cur = abs(implied / e_cur - 1) <= tol
    near_prev = (e_prev is not None) and abs(implied / e_prev - 1) <= tol
    if near_cur and near_prev:
        return 'indeterminate'
    if near_cur:
        return 'current'
    if near_prev:
        return 'PREVIOUS'
    return 'other'


def settled(frames):
    """Mean of the last two good frames of a series (the series is long enough to be settled)."""
    good = [f for f in frames if f.ok]
    if len(good) < 2:
        raise ExperimentError('too few valid frames')
    return float(np.mean([good[-1].mean, good[-2].mean]))


def make_row(exp, fr, e_cur, e_prev, k, dark, **kw):
    implied = (fr.mean - dark) / k if (fr.ok and k) else float('nan')
    v = 'invalid' if (not fr.ok) else ('saturated' if fr.saturated else verdict(implied, e_cur, e_prev))
    row = dict(exp=exp, channel='', delay_s='', round='', j='', E_cur_ms=e_cur, E_prev_ms=e_prev,
               mean=fr.mean, dark=dark, implied_ms=implied,
               implied_over_cur=(implied / e_cur if e_cur else float('nan')), verdict=v,
               frame_id=fr.frame_id, readback_ms=fr.readback_ms, saturated=fr.saturated,
               t_trigger_s=round(fr.t_trigger - T0, 4) if fr.t_trigger else '',
               trig_to_read_ms=round((fr.t_done - fr.t_trigger) * 1e3, 1) if fr.t_done else '', err=fr.err)
    row.update(kw)
    return row


def check_frames(frames, what):
    for f in frames:
        if not f.ok:
            raise ExperimentError(f'{what}: frame failed ({f.err})')
        if f.saturated:
            raise ExperimentError(f'{what}: image is saturated - lower the illumination intensity or the exposure')


def check_signal(mean, dark, what):
    if mean - dark < MIN_SIGNAL_COUNTS:
        raise ExperimentError(f'{what}: signal is only {mean - dark:.1f} counts above dark - '
                              f'raise intensity/exposure or use a brighter sample')


# ------------------------------------------------------------------------------------------------
# experiments; `rig` must provide: info() channels() get_config_exposure(n) set_config_exposure(n,ms)
# switch_channel(n) set_camera_exposure(ms) readback_exposure_ms() grab(illum=True) sleep(s)
# ------------------------------------------------------------------------------------------------
def exp1_register(rig, exposures):
    out = []
    seq = list(exposures) + list(reversed(exposures))
    for e in seq:
        t = time.perf_counter()
        rig.set_camera_exposure(e)
        call_ms = (time.perf_counter() - t) * 1e3
        rb0 = rig.readback_exposure_ms()
        rig.sleep(0.05)
        rb1 = rig.readback_exposure_ms()
        out.append(dict(requested_ms=e, readback_immediate_ms=rb0, readback_after_50ms_ms=rb1, set_call_ms=round(call_ms, 2)))

    def bad(r, key):
        v = r[key]
        return v is not None and abs(v - r['requested_ms']) > max(0.05 * r['requested_ms'], 0.05)
    return dict(rows=out, available=any(r['readback_immediate_ms'] is not None for r in out),
                mismatch_immediate=any(bad(r, 'readback_immediate_ms') for r in out),
                mismatch_after_50ms=any(bad(r, 'readback_after_50ms_ms') for r in out))


def exp2_direct(rig, channel, ea, eb, delays, n_pairs, rows):
    rig.switch_channel(channel)
    cal = {}
    for e in (ea, eb):
        rig.set_camera_exposure(e)
        rig.sleep(0.5)
        fr = [rig.grab() for _ in range(4)]
        dk = [rig.grab(illum=False) for _ in range(2)]
        check_frames(fr + dk, f'E2 calibration at {e} ms')
        i_set, dark = settled(fr), dk[-1].mean
        check_signal(i_set, dark, f'E2 calibration at {e} ms')
        cal[e] = dict(I=i_set, dark=dark, k=(i_set - dark) / e)
    k = float(np.mean([cal[ea]['k'], cal[eb]['k']]))
    lin = cal[ea]['k'] / cal[eb]['k']
    prev = eb
    for d in delays:
        for i in range(2 * n_pairs):
            e = ea if i % 2 == 0 else eb
            rig.set_camera_exposure(e)
            rig.sleep(d)
            fr = rig.grab()
            rows.append(make_row('E2', fr, e, prev, k, cal[e]['dark'], channel=channel, delay_s=d, round=i // 2))
            prev = e
    return dict(channel=channel, ea=ea, eb=eb, cal=cal, k=k, linearity_kA_over_kB=lin)


def calibrate_channels(rig, names, exposures):
    calib = {}
    for c in names:
        rig.switch_channel(c)
        rig.sleep(0.5)
        fr = [rig.grab() for _ in range(4)]
        dk = [rig.grab(illum=False) for _ in range(2)]
        check_frames(fr + dk, f'calibration of {c}')
        i_set, dark = settled(fr), dk[-1].mean
        check_signal(i_set, dark, f'calibration of {c}')
        e = exposures[c]
        calib[c] = dict(E=e, I=i_set, dark=dark, k=(i_set - dark) / e,
                        first_frame_after_calibration_switch_over_settled=(fr[0].mean - dark) / (i_set - dark))
    return calib


def exp3_switch(rig, names, exposures, calib, delays, rounds, frames_per_switch, rows):
    n = len(names)
    for d in delays:
        for r in range(rounds):
            for idx, c in enumerate(names):
                prev_c = names[(idx - 1) % n]
                rig.switch_channel(c)
                rig.sleep(d)
                for j in range(frames_per_switch):
                    fr = rig.grab()
                    rows.append(make_row('E3', fr, exposures[c], exposures[prev_c], calib[c]['k'], calib[c]['dark'],
                                         channel=c, delay_s=d, round=r, j=j))


def exp3b_dummy(rig, names, exposures, calib, rounds, rows):
    n = len(names)
    cost = []
    for r in range(rounds):
        for idx, c in enumerate(names):
            prev_c = names[(idx - 1) % n]
            t = time.perf_counter()
            rig.switch_channel(c)
            rig.grab(illum=False)
            t_dummy = time.perf_counter() - t
            fr = rig.grab()
            rows.append(make_row('E3b', fr, exposures[c], exposures[prev_c], calib[c]['k'], calib[c]['dark'],
                                 channel=c, delay_s=0, round=r, j='after_dummy'))
            cost.append(t_dummy)
    return dict(dummy_cost_ms_median=float(np.median(cost) * 1e3) if cost else None)


def analyze_sequence(seq, exposures, calib, first_prev_exposure=None, exp='E5'):
    """seq: [(channel, mean_image, extra_dict)] in acquisition order -> rows"""
    rows = []
    prev_e = first_prev_exposure
    for i, (c, mean, extra) in enumerate(seq):
        fr = Frame(mean=mean)
        row = make_row(exp, fr, exposures[c], prev_e, calib[c]['k'], calib[c]['dark'], channel=c, round=i)
        row.update(extra)
        rows.append(row)
        prev_e = exposures[c]
    return rows


def count(rows, **flt):
    sel = [r for r in rows if all(r.get(k) == v for k, v in flt.items())]
    c = {v: sum(1 for r in sel if r['verdict'] == v) for v in ('current', 'PREVIOUS', 'other', 'indeterminate', 'saturated', 'invalid')}
    good = [r['implied_over_cur'] for r in sel if r['verdict'] not in ('invalid', 'saturated') and np.isfinite(r['implied_over_cur'])]
    c['n'] = len(sel)
    c['median_implied_over_cur'] = float(np.median(good)) if good else float('nan')
    c['frac_prev'] = c['PREVIOUS'] / max(1, c['current'] + c['PREVIOUS'] + c['other'])
    c['frac_cur'] = c['current'] / max(1, c['current'] + c['PREVIOUS'] + c['other'])
    return c


def interpret(res, rows, delays):
    h = []
    e1 = res.get('E1')
    if e1 and e1['available']:
        if e1['mismatch_immediate'] or e1['mismatch_after_50ms']:
            h.append('E1: the camera register does NOT read back the requested exposure -> the exposure is not (correctly) '
                     'written to the camera (driver/units/limits). Look at camera.set_exposure_time first.')
        else:
            h.append('E1: the camera register reads back the requested exposure immediately -> setting the value works.')
    elif e1:
        h.append('E1: no register read-back available on this camera (simulation?) - skipped.')
    d0, dmax = min(delays), max(delays)
    e2_0, e2_max = count(rows, exp='E2', delay_s=d0), count(rows, exp='E2', delay_s=dmax)
    if e2_0['n']:
        if e2_0['frac_prev'] >= 0.6 and e2_max['frac_prev'] >= 0.6:
            h.append(f'E2: even with a single channel and a wait of {dmax} s between set_exposure and trigger the frame has the '
                     f'PREVIOUS exposure -> the camera applies a new exposure only from the 2nd frame after the change, '
                     f'independent of waiting time (frame-latched exposure, or one stale frame in the camera/driver queue). '
                     f'Not a Qt/software-ordering problem. Check the frame_id column (stale frame?) and E3b (dummy frame).')
        elif e2_0['frac_prev'] >= 0.6 and e2_max['frac_cur'] >= 0.8:
            lag_free = [d for d in delays if count(rows, exp='E2', delay_s=d)['frac_cur'] >= 0.8]
            h.append(f'E2: the PREVIOUS exposure is used when the trigger follows set_exposure immediately, but the correct one '
                     f'after waiting (correct from {min(lag_free) if lag_free else dmax} s) -> a timing problem: the new exposure '
                     f'needs time to become effective.')
        elif e2_0['frac_cur'] >= 0.8:
            h.append('E2: with one channel and direct set_exposure_time the exposure is correct at all waits -> camera and driver are fine.')
        else:
            h.append(f'E2: unclear result (current {e2_0["current"]}, previous {e2_0["PREVIOUS"]}, other {e2_0["other"]}).')
    e3_0 = count(rows, exp='E3', delay_s=d0, j=0)
    e3_1 = count(rows, exp='E3', delay_s=d0, j=1)
    e3_2 = count(rows, exp='E3', delay_s=d0, j=2)
    if e3_0['n']:
        if e3_0['frac_prev'] >= 0.6:
            lag = 1 if e3_1['frac_cur'] >= 0.6 else (2 if e3_2['frac_cur'] >= 0.6 else 3)
            h.append(f'E3: after a channel switch through the real signal path the first frame has the PREVIOUS channel\'s exposure; '
                     f'the exposure is correct from frame index {lag} on (lag = {lag} frame(s)). This reproduces the bug.')
        elif e3_0['frac_cur'] >= 0.8:
            h.append('E3: the first frame after a channel switch has the correct exposure in this test (bug NOT reproduced here).')
        else:
            h.append(f'E3: unclear (current {e3_0["current"]}, previous {e3_0["PREVIOUS"]}, other {e3_0["other"]}).')
        e3_max = count(rows, exp='E3', delay_s=dmax, j=0)
        if e3_max['n'] and e3_0['frac_prev'] >= 0.6 and e3_max['frac_cur'] >= 0.8:
            h.append(f'E3: with a wait of {dmax} s after the switch the first frame is correct -> timing, see E2/E3 per-delay table.')
        if e2_0['n'] and e2_0['frac_cur'] >= 0.8 and e3_0['frac_prev'] >= 0.6:
            h.append('E2 correct but E3 wrong -> the problem sits in the channel-switch path (signal/set_microscope_mode/widgets/'
                     'illumination/filter wheel), not in camera.set_exposure_time itself.')
    e3b = count(rows, exp='E3b')
    if e3b['n']:
        h.append(f'E3b: with one discarded dark frame after the switch the next frame is correct in {e3b["current"]} of {e3b["n"]} cases.')
    e5 = count(rows, exp='E5')
    if e5['n']:
        h.append(f'E5 (real acquisition): {e5["current"]} frames with current exposure, {e5["PREVIOUS"]} with PREVIOUS, '
                 f'{e5["other"]} other, {e5["indeterminate"]} indeterminate of {e5["n"]}.')
        if e3_0['n'] and e3_0['frac_cur'] >= 0.8 and e5['PREVIOUS'] > e5['current']:
            h.append('E3 correct but the real acquisition wrong -> something in the real worker/acquisition code differs from this '
                     'sequence; compare events.csv (order and timing of set exposure / trigger / read).')
    return h


# ------------------------------------------------------------------------------------------------
# report
# ------------------------------------------------------------------------------------------------
def fmt(v, nd=2):
    if v is None:
        return 'n/a'
    if isinstance(v, float):
        return 'nan' if not np.isfinite(v) else f'{v:.{nd}f}'
    return str(v)


def build_report(args, env, res, rows, delays, warnings, hints, elapsed):
    L = []
    a = L.append
    a('EXPOSURE DIAGNOSTIC REPORT')
    a('=' * 78)
    a(f'date      : {datetime.datetime.now():%Y-%m-%d %H:%M:%S}   duration {elapsed:.0f} s')
    a(f'command   : {" ".join(sys.argv)}')
    a('')
    a('ENVIRONMENT')
    for k, v in env.items():
        a(f'  {k:28s}: {v}')
    a('')
    cal = res.get('calib')
    if cal:
        a('CHANNELS (exposure used in the test; brightness model: counts = dark + k*exposure)')
        a(f'  {"channel":32s} {"E ms":>8s} {"settled":>9s} {"dark":>8s} {"k/ms":>9s} {"1st frame/settled":>18s}')
        for c, v in cal.items():
            a(f'  {c:32s} {v["E"]:8.2f} {v["I"]:9.1f} {v["dark"]:8.1f} {v["k"]:9.2f} {v["first_frame_after_calibration_switch_over_settled"]:18.2f}')
        a('')
    e1 = res.get('E1')
    if e1:
        a('E1  camera register read-back (requested vs read back, ms)')
        for r in e1['rows']:
            a(f'  requested {r["requested_ms"]:8.2f}   immediate {fmt(r["readback_immediate_ms"]):>9s}   after 50 ms {fmt(r["readback_after_50ms_ms"]):>9s}   set call {r["set_call_ms"]} ms')
        a('')
    if any(r['exp'] == 'E2' for r in rows):
        e2 = res['E2']
        a(f'E2  direct set_exposure_time, channel "{e2["channel"]}", exposure alternating {e2["ea"]} / {e2["eb"]} ms; '
          f'linearity kA/kB = {e2["linearity_kA_over_kB"]:.2f} (should be ~1)')
        a(f'  {"wait set->trigger":>18s} {"n":>4s} {"current":>8s} {"PREVIOUS":>9s} {"other":>6s} {"indet.":>7s} {"median implied/requested":>26s}')
        for d in delays:
            c = count(rows, exp='E2', delay_s=d)
            a(f'  {d:15.2f} s {c["n"]:4d} {c["current"]:8d} {c["PREVIOUS"]:9d} {c["other"]:6d} {c["indeterminate"]:7d} {c["median_implied_over_cur"]:26.2f}')
        a('')
    if any(r['exp'] == 'E3' for r in rows):
        a('E3  channel switch through the real signal path (j = frame index after the switch; the real acquisition uses j=0)')
        a(f'  {"wait":>6s} {"j":>2s} {"n":>4s} {"current":>8s} {"PREVIOUS":>9s} {"other":>6s} {"indet.":>7s} {"median implied/requested":>26s}')
        for d in delays:
            for j in sorted({r['j'] for r in rows if r['exp'] == 'E3'}):
                c = count(rows, exp='E3', delay_s=d, j=j)
                a(f'  {d:5.2f}s {j:2d} {c["n"]:4d} {c["current"]:8d} {c["PREVIOUS"]:9d} {c["other"]:6d} {c["indeterminate"]:7d} {c["median_implied_over_cur"]:26.2f}')
        a('')
        a('  per channel, j=0, wait=%.2f s:' % min(delays))
        for ch in res['calib']:
            c = count(rows, exp='E3', delay_s=min(delays), j=0, channel=ch)
            a(f'    {ch:32s} current {c["current"]:3d}  PREVIOUS {c["PREVIOUS"]:3d}  other {c["other"]:3d}  indet. {c["indeterminate"]:3d}  median implied/requested {c["median_implied_over_cur"]:.2f}')
        a('')
    if any(r['exp'] == 'E3b' for r in rows):
        c = count(rows, exp='E3b')
        a(f'E3b one discarded dark frame after the switch: current {c["current"]}, PREVIOUS {c["PREVIOUS"]}, other {c["other"]} '
          f'of {c["n"]}; median cost of switch + dummy frame {fmt(res["E3b"]["dummy_cost_ms_median"], 0)} ms')
        a('')
    if any(r['exp'] == 'E5' for r in rows):
        c = count(rows, exp='E5')
        a(f'E5  real acquisition ({res.get("E5_info", "")})')
        a(f'  frames: current {c["current"]}, PREVIOUS {c["PREVIOUS"]}, other {c["other"]}, indeterminate {c["indeterminate"]} of {c["n"]}')
        a(f'  {"t":>3s} {"channel":32s} {"requested":>10s} {"previous":>9s} {"implied":>9s}  verdict')
        for r in [r for r in rows if r['exp'] == 'E5'][:40]:
            a(f'  {str(r.get("t", "")):>3s} {r["channel"]:32s} {r["E_cur_ms"]:10.2f} {fmt(r["E_prev_ms"]):>9s} {fmt(r["implied_ms"]):>9s}  {r["verdict"]}')
        a('')
    fids = [r['frame_id'] for r in rows if isinstance(r['frame_id'], (int, float)) and r['exp'] in ('E2', 'E3')]
    if len(fids) > 3:
        d = np.diff(fids)
        a(f'frame IDs (E2/E3 grabs): {len(fids)} frames, steps other than +1: {int(np.sum(d != 1))} '
          f'(a step of 0 or <0 or >1 would indicate stale/dropped frames)')
        a('')
    a('AUTOMATIC INTERPRETATION (hints, not proof)')
    for h in hints:
        a('  - ' + h)
    if warnings:
        a('')
        a('WARNINGS / NOT RUN')
        for w in warnings:
            a('  ! ' + w)
    a('')
    a('Please send this file (report.txt), frames.csv and events.csv.')
    return '\n'.join(L)


def write_outputs(out_dir, args, env, res, rows, events, delays, warnings, elapsed):
    hints = interpret(res, rows, delays)
    text = build_report(args, env, res, rows, delays, warnings, hints, elapsed)
    with open(os.path.join(out_dir, 'report.txt'), 'w', encoding='utf-8') as f:
        f.write(text)
    if rows:
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(os.path.join(out_dir, 'frames.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    if events:
        with open(os.path.join(out_dir, 'events.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['t_s', 'thread', 'what', 'arg'])
            w.writeheader()
            w.writerows(events)

    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            return None if not np.isfinite(o) else float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        return o
    with open(os.path.join(out_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(clean(dict(env=env, results=res, hints=hints, warnings=warnings)), f, indent=1, default=str)
    return text


def run_experiments(rig, names, exposures, delays, rounds, warnings, rows):
    """All experiments that run in the worker thread. Each experiment is isolated: one failing does not stop the others."""
    res = {}
    lo, hi = min(exposures[c] for c in names), max(exposures[c] for c in names)
    try:
        res['E1'] = exp1_register(rig, [lo, hi, (lo + hi) / 2])
    except Exception as e:
        warnings.append(f'E1 failed: {e!r}')
    try:
        res['calib'] = calibrate_channels(rig, names, exposures)
    except Exception as e:
        warnings.append(f'calibration failed - E3/E3b/E5 skipped: {e}')
        traceback.print_exc()
    try:
        res['E2'] = exp2_direct(rig, names[0], lo, hi, delays, max(3, rounds // 2), rows)
    except Exception as e:
        warnings.append(f'E2 failed: {e}')
        traceback.print_exc()
    if 'calib' in res:
        try:
            exp3_switch(rig, names, exposures, res['calib'], delays, rounds, 3, rows)
        except Exception as e:
            warnings.append(f'E3 failed: {e}')
            traceback.print_exc()
        try:
            res['E3b'] = exp3b_dummy(rig, names, exposures, res['calib'], rounds, rows)
        except Exception as e:
            warnings.append(f'E3b failed: {e}')
            traceback.print_exc()
    return res


# ------------------------------------------------------------------------------------------------
# real microscope / simulation rig (needs Qt + the GUI)
# ------------------------------------------------------------------------------------------------
FULL_SCALE = {'MONO8': 255, 'MONO10': 1023, 'MONO12': 65535, 'MONO14': 16383, 'MONO16': 65535,
              'BAYER_RG8': 255, 'BAYER_RG12': 65535}


class GuiRig:
    def __init__(self, win, app, simulation):
        from qtpy.QtCore import QObject, Signal, Qt
        self.win, self.app, self.simulation = win, app, simulation
        self.live = win.liveController
        self.camera = win.camera
        self.mcu = self.live.microcontroller
        self.mpc = win.multipointController
        self.cfg = {c.name: c for c in win.configurationManager.configurations}
        self._orig_exposure = {n: c.exposure_time for n, c in self.cfg.items()}
        self.events = []
        self._last_frame_id = None
        self._patches = []

        class Emitter(QObject):
            sig = Signal(object)
        self._emitter = Emitter()
        # same connection type and receiver as MultiPointController uses for the worker's signal
        self._emitter.sig.connect(self.mpc.slot_current_configuration, type=Qt.BlockingQueuedConnection)

    # --- info
    def channels(self):
        return list(self.cfg)

    def get_config_exposure(self, n):
        return self.cfg[n].exposure_time

    def set_config_exposure(self, n, ms):
        self.cfg[n].exposure_time = float(ms)       # in memory only

    def restore_config_exposures(self):
        for n, e in self._orig_exposure.items():
            self.cfg[n].exposure_time = e

    def info(self):
        cam = self.camera
        d = dict(camera_class=type(cam).__name__, trigger_mode=str(getattr(self.live, 'trigger_mode', None)),
                 pixel_format=getattr(cam, 'pixel_format', None), is_streaming=getattr(cam, 'is_streaming', None),
                 callback_enabled_at_start=getattr(cam, 'callback_is_enabled', None),
                 live_on_at_start=getattr(self.live, 'is_live', None),
                 control_illumination=getattr(self.live, 'control_illumination', None),
                 camera_exposure_attr_ms=getattr(cam, 'exposure_time', None),
                 is_global_shutter=getattr(cam, 'is_global_shutter', None))
        for key, expr in (('camera_model', 'DeviceModelName'), ('camera_sn', 'DeviceSerialNumber')):
            try:
                d[key] = getattr(cam.camera, expr).get()
            except Exception:
                pass
        try:
            from control._def import TriggerMode
            d['software_trigger'] = (self.live.trigger_mode == TriggerMode.SOFTWARE)
        except Exception:
            pass
        return d

    # --- low level
    def mcu_wait(self):
        from control._def import SLEEP_TIME_S
        while self.mcu.is_busy():
            time.sleep(SLEEP_TIME_S)

    def sleep(self, s):
        if s > 0:
            time.sleep(s)

    def set_camera_exposure(self, ms):
        self.camera.set_exposure_time(ms)

    def readback_exposure_ms(self):
        try:
            return float(self.camera.camera.ExposureTime.get()) / 1000.0
        except Exception:
            return None

    def full_scale(self):
        return FULL_SCALE.get(getattr(self.camera, 'pixel_format', 'MONO12'), 65535)

    def switch_channel(self, name):
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('switch_channel must run in the worker thread (BlockingQueuedConnection)')
        self._emitter.sig.emit(self.cfg[name])
        self.mcu_wait()

    def grab(self, illum=True):
        f = Frame()
        try:
            if illum:
                self.live.turn_on_illumination()
                self.mcu_wait()
            f.t_trigger = time.perf_counter()
            self.camera.send_trigger()
            img = self.camera.read_frame()
            f.t_done = time.perf_counter()
            if illum:
                self.live.turn_off_illumination()
        except Exception as e:
            f.ok, f.err = False, repr(e)
            try:
                self.live.turn_off_illumination()
            except Exception:
                pass
            return f
        if img is None:
            f.ok, f.err = False, 'read_frame returned None'
            return f
        sub = img[::4, ::4]
        f.mean = float(np.mean(img, dtype=np.float64))
        f.p999 = float(np.percentile(sub, 99.9))
        f.saturated = (not self.simulation) and f.p999 >= 0.97 * self.full_scale()
        f.frame_id = self._last_frame_id
        f.readback_ms = self.readback_exposure_ms()
        return f

    # --- hooks (frame id + event log), always removed again
    def install_hooks(self):
        t0 = time.perf_counter()

        def log_ev(what, arg=''):
            self.events.append(dict(t_s=round(time.perf_counter() - t0, 5), thread=threading.current_thread().name, what=what, arg=arg))

        def patch(obj, name, wrapper_factory):
            try:
                orig = getattr(obj, name)
                new = wrapper_factory(orig)
                setattr(obj, name, new)
                self._patches.append((obj, name))
            except Exception as e:
                log(f'  (hook {name} not installed: {e!r})')

        def wrap(what, argfmt=lambda a, k: ''):
            def factory(orig):
                def w(*a, **k):
                    log_ev(what + ' >', argfmt(a, k))
                    r = orig(*a, **k)
                    log_ev(what + ' <', '')
                    return r
                return w
            return factory

        patch(self.camera, 'send_trigger', wrap('camera.send_trigger'))
        patch(self.camera, 'read_frame', wrap('camera.read_frame'))
        patch(self.camera, 'set_exposure_time', wrap('camera.set_exposure_time', lambda a, k: str(a[0]) if a else ''))
        patch(self.live, 'set_microscope_mode', wrap('live.set_microscope_mode', lambda a, k: getattr(a[0], 'name', '') if a else ''))
        patch(self.live, 'turn_on_illumination', wrap('live.turn_on_illumination'))
        patch(self.live, 'turn_off_illumination', wrap('live.turn_off_illumination'))
        # register level (catches every write, whoever calls it) and frame id
        try:
            feat = self.camera.camera.ExposureTime
            orig_set = feat.set

            def set_reg(v, *a, **k):
                log_ev('REGISTER ExposureTime.set', f'{v / 1000.0:.3f} ms')
                return orig_set(v, *a, **k)
            feat.set = set_reg
            if getattr(self.camera.camera.ExposureTime, 'set', None) is set_reg:
                self._patches.append((feat, 'set'))
            else:
                log('  (register-level hook not persistent on this driver; python-level calls are logged)')
        except Exception as e:
            log(f'  (register-level hook not available: {e!r})')
        try:
            ds = self.camera.camera.data_stream[self.camera.device_index]
            orig_get = ds.get_image

            def get_image(*a, **k):
                raw = orig_get(*a, **k)
                try:
                    self._last_frame_id = raw.get_frame_id()
                except Exception:
                    self._last_frame_id = None
                return raw
            ds.get_image = get_image
            self._patches.append((ds, 'get_image'))
        except Exception as e:
            log(f'  (frame-id hook not available: {e!r})')

    def remove_hooks(self):
        for obj, name in self._patches:
            try:
                delattr(obj, name)
            except Exception:
                pass
        self._patches = []

    # --- worker thread
    def run_in_worker(self, fn):
        from qtpy.QtCore import QObject, QThread, QEventLoop, Signal

        class W(QObject):
            done = Signal()

            def __init__(s):
                super().__init__()
                s.result, s.exc = None, None

            def run(s):
                try:
                    s.result = fn()
                except BaseException:
                    s.exc = traceback.format_exc()
                s.done.emit()
        thread = QThread()
        thread.setObjectName('diag-worker')
        w = W()
        w.moveToThread(thread)
        loop = QEventLoop()
        thread.started.connect(w.run)
        w.done.connect(loop.quit)
        w.done.connect(thread.quit)
        thread.start()
        loop.exec_()
        thread.wait()
        if w.exc:
            raise RuntimeError(w.exc)
        return w.result


def read_image_mean(path):
    try:
        import tifffile
        return float(np.mean(tifffile.imread(path), dtype=np.float64))
    except Exception:
        import imageio
        return float(np.mean(imageio.imread(path), dtype=np.float64))


def run_real_acquisition(win, app, rig, names, exposures, calib, rounds, save_dir, timeout_s=900):
    from qtpy.QtCore import QEventLoop, QTimer, Qt
    mp = getattr(win, 'multiPointWidget2', None)
    if mp is None:
        raise ExperimentError('Flexible Multipoint widget not available')
    mpc = win.multipointController
    first_prev = rig.live.currentConfiguration.exposure_time if rig.live.currentConfiguration is not None else None
    mp.clear()
    app.processEvents()
    mp.add_location()                       # current position, no stage motion
    app.processEvents()
    mp.lineEdit_savingDir.setText(save_dir)
    mpc.set_base_path(save_dir)
    mp.lineEdit_experimentID.setText('expdiag')
    mp.entry_NX.setValue(1); mp.entry_NY.setValue(1); mp.entry_NZ.setValue(1)
    mp.entry_Nt.setValue(rounds); mp.entry_dt.setValue(0)
    mp.list_configurations.clearSelection()
    for n in names:
        for item in mp.list_configurations.findItems(n, Qt.MatchExactly):
            item.setSelected(True)
    mp.checkbox_withAutofocus.setChecked(False)
    mp.checkbox_save_multichannel.setChecked(False)
    app.processEvents()
    loop = QEventLoop()
    mpc.acquisitionFinished.connect(loop.quit)
    QTimer.singleShot(int(timeout_s * 1000), loop.quit)
    t0 = time.time()
    mp.btn_startAcquisition.click()
    loop.exec_()
    finished = (time.time() - t0) < timeout_s - 1
    try:
        mpc.acquisitionFinished.disconnect(loop.quit)
    except TypeError:
        pass
    if not finished:
        mpc.request_abort_aquisition()
    end = time.time() + 1.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    cands = [d for d in glob.glob(os.path.join(save_dir, 'expdiag_*')) if os.path.isdir(d)]
    if not cands:
        raise ExperimentError('acquisition produced no experiment folder')
    exp_dir = max(cands, key=os.path.getmtime)
    seq = []
    t_dirs = sorted([d for d in os.listdir(exp_dir) if d.isdigit()], key=int)
    for t in t_dirs:
        files = [f for f in glob.glob(os.path.join(exp_dir, t, '*')) if f.lower().endswith(('.tiff', '.tif', '.png', '.bmp', '.jpg'))
                 and 'focus_camera' not in f and 'laser af' not in f]
        for f in sorted(files, key=os.path.getmtime):
            tag = os.path.basename(f)
            ch = next((n for n in names if '_' + n.replace(' ', '_') + '.' in tag), None)
            if ch is not None:
                seq.append((ch, read_image_mean(f), dict(t=int(t), file=os.path.basename(f))))
    expected = rounds * len(names)
    rows = analyze_sequence(seq, exposures, calib, first_prev_exposure=first_prev)
    return rows, dict(finished=finished, n_images=len(seq), expected_images=expected, folder=exp_dir)


def environment(extra):
    env = dict(python=sys.version.split()[0], platform=platform.platform(), numpy=np.__version__)
    try:
        env['git_commit'] = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=SOFTWARE_DIR, text=True).strip()
        env['git_branch'] = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=SOFTWARE_DIR, text=True).strip()
    except Exception:
        pass
    env.update(extra)
    return env


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    m = ap.add_mutually_exclusive_group(required=True)
    m.add_argument('--simulation', action='store_true', help='simulated hardware: checks the plumbing only (results meaningless)')
    m.add_argument('--hardware', action='store_true', help='real microscope (no stage motion; illumination flashes)')
    ap.add_argument('--out-dir', required=True, help='existing folder; a subfolder exposure_diag_<time> is created')
    ap.add_argument('--channels', default='', help='comma separated configuration names (2-3 recommended); default: first 3')
    ap.add_argument('--exposures', default='10,40,160', help='ms per channel in order, or "keep" for the configured values')
    ap.add_argument('--rounds', type=int, default=6, help='cycles through the channels per setting')
    ap.add_argument('--delays', default='0,0.2,1.0', help='waits (s) between channel switch / set_exposure and the trigger')
    ap.add_argument('--skip-real', action='store_true', help='skip E5 (real acquisition with the MultiPoint worker)')
    ap.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    args = ap.parse_args()
    if not os.path.isdir(args.out_dir):
        sys.exit(f'Output directory does not exist: {args.out_dir}')
    delays = sorted({float(x) for x in args.delays.split(',')})
    out_dir = os.path.join(os.path.abspath(args.out_dir), 'exposure_diag_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(out_dir)

    os.chdir(SOFTWARE_DIR)
    sys.path.insert(0, SOFTWARE_DIR)
    os.environ['QT_API'] = 'pyqt5'
    from qtpy.QtWidgets import QApplication
    import control.gui_hcs as gui

    app = QApplication([])
    app.setStyle('Fusion')
    win = gui.OctopiGUI(is_simulation=args.simulation)
    win.show()
    app.processEvents()
    rig = GuiRig(win, app, args.simulation)

    all_names = rig.channels()
    names = [s.strip() for s in args.channels.split(',') if s.strip()] or all_names[:3]
    bad = [n for n in names if n not in all_names]
    if bad or len(names) < 2:
        sys.exit(f'Need >= 2 valid channels. Unknown: {bad}. Available: {all_names}')
    if args.exposures.strip().lower() == 'keep':
        exposures = {n: rig.get_config_exposure(n) for n in names}
    else:
        ev = [float(x) for x in args.exposures.split(',')]
        exposures = {n: ev[i % len(ev)] for i, n in enumerate(names)}
    warnings = []
    cyc = [exposures[names[i]] / exposures[names[(i - 1) % len(names)]] for i in range(len(names))]
    if any(max(r, 1 / r) < 1.5 for r in cyc):
        warnings.append('two consecutive channels have exposures within a factor 1.5 -> the test cannot tell them apart '
                        '(use --exposures with larger differences)')
    info = rig.info()
    log('=' * 70)
    log('mode      : ' + ('REAL HARDWARE' if args.hardware else 'simulation'))
    log('channels  : ' + ', '.join(f'{n} ({exposures[n]} ms)' for n in names))
    log('camera    : ' + str({k: info[k] for k in ('camera_class', 'trigger_mode', 'pixel_format') if k in info}))
    log('delays    : ' + str(delays) + '   rounds: ' + str(args.rounds))
    log('The illumination will flash many times; the stage does not move. Sample should be in focus, static, not saturated.')
    log('=' * 70)
    if info.get('software_trigger') is False:
        sys.exit('The camera is not in SOFTWARE trigger mode; this test replicates the software-trigger acquisition path only.')
    if args.hardware and not args.yes and input('Type YES to continue: ').strip() != 'YES':
        sys.exit('Aborted.')

    cfg_file = os.path.join(SOFTWARE_DIR, win.configurationManager.config_filename)
    cfg_backup = None
    if os.path.isfile(cfg_file):
        cfg_backup = os.path.join(out_dir, 'channel_configurations_backup.xml')
        shutil.copy2(cfg_file, cfg_backup)

    # state like during a multipoint acquisition: live off, camera callback off
    orig_cfg = rig.live.currentConfiguration
    was_live = rig.live.is_live
    cb_was = getattr(rig.camera, 'callback_is_enabled', False)
    if was_live:
        rig.live.stop_live()
    if cb_was:
        rig.camera.disable_callback()
    for n in names:
        rig.set_config_exposure(n, exposures[n])
    rows, res = [], {}
    t_start = time.time()
    try:
        rig.install_hooks()
        res = rig.run_in_worker(lambda: run_experiments(rig, names, exposures, delays, args.rounds, warnings, rows))
    except Exception:
        warnings.append('worker failed: ' + traceback.format_exc().splitlines()[-1])
        traceback.print_exc()
    finally:
        rig.remove_hooks()
        try:
            if cb_was:
                rig.camera.enable_callback()
            if orig_cfg is not None:
                win.liveControlWidget.set_microscope_mode(orig_cfg)
            if was_live:
                rig.live.start_live()
        except Exception as e:
            warnings.append(f'could not fully restore live state: {e!r}')
    if 'calib' in res and not args.skip_real:
        try:
            rig.events = []
            rig.install_hooks()
            r5, info5 = run_real_acquisition(win, app, rig, names, exposures, res['calib'], args.rounds, out_dir)
            rows.extend(r5)
            res['E5_info'] = f'{info5["n_images"]} of {info5["expected_images"]} images analysed, finished={info5["finished"]}'
            if info5['n_images'] != info5['expected_images']:
                warnings.append('E5: number of analysed images differs from expected: ' + res['E5_info'])
        except Exception as e:
            warnings.append(f'E5 failed: {e}')
            traceback.print_exc()
        finally:
            rig.remove_hooks()
    rig.restore_config_exposures()
    try:
        if orig_cfg is not None:
            win.liveControlWidget.set_microscope_mode(orig_cfg)
    except Exception:
        pass
    if cfg_backup and os.path.isfile(cfg_file) and open(cfg_file, 'rb').read() != open(cfg_backup, 'rb').read():
        shutil.copy2(cfg_backup, cfg_file)
        warnings.append('channel_configurations.xml had been modified during the test and was restored from the backup')

    env = environment(info)
    env['channels'] = ', '.join(f'{n} @ {exposures[n]} ms' for n in names)
    text = write_outputs(out_dir, args, env, res, rows, rig.events, delays, warnings, time.time() - t_start)
    log('\n' + text)
    log('\nOutput folder: ' + out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
