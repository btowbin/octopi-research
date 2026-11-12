"""User-level scripting API for JOBs: the `scope` object a job script receives.

A job is a plain Python file that defines  run_job(scope).  It describes WHAT is done and WHEN
(loops over points and time points, channels, autofocus, waiting). It never talks to hardware:
everything goes through the high-level methods of `scope`; the hardware is driven by a backend
(control/jobs/hardware_backend.py) that reuses the tested movement/imaging/autofocus code.

This module has no hardware and no Qt dependency, so jobs can be checked (dry run) and tested anywhere.
"""
import os
import re
import sys
import csv
import time
import datetime

import numpy as np

from control.jobs import stackio

AF_MAX_Z_DRIFT_MM = 0.5     # as in the standard acquisition: a larger focus shift is not stored for later visits
MAX_STACK_SPAN_UM = 2000.0  # a z-stack may not be larger than this (safety against typing mm instead of um)
MAX_BUFFER_BYTES = 4 * 1024 ** 3   # a z-stack or movie is held in memory until it is written: refuse more than this


class JobError(Exception):
    """The job is wrong (unknown channel, bad argument, no run_job, ...) or could not be executed."""


class JobAborted(BaseException):
    """Raised inside the job when the user aborts. Derives from BaseException so that a job's own
    `except Exception:` does not swallow it."""


class DryRunLimit(BaseException):
    """Raised inside a DRY RUN only (never in a real run) when the job has done more scope operations than the
    test allows. Not an error of the job: the runner reports the dry run as 'only partly tested'.
    BaseException for the same reason as JobAborted."""
    def __init__(self, n_operations):
        super().__init__(n_operations)
        self.n_operations = n_operations


# ------------------------------------------------------------------------------------------------
# clocks (real and simulated, the latter for dry runs and tests)
# ------------------------------------------------------------------------------------------------
class RealClock:
    def __init__(self):
        self._t0 = time.monotonic()

    def now(self):
        return time.monotonic() - self._t0

    def sleep(self, seconds):
        if seconds > 0:
            time.sleep(seconds)

    def wait_until(self, t_abs, should_abort):
        """Sleep until now() >= t_abs in short steps; returns False if aborted."""
        while True:
            if should_abort():
                return False
            remaining = t_abs - self.now()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.1))


class SimClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)

    def wait_until(self, t_abs, should_abort):
        if should_abort():
            return False
        self.t = max(self.t, t_abs)
        return True


# ------------------------------------------------------------------------------------------------
# data classes
# ------------------------------------------------------------------------------------------------
class Point:
    """One imaging position. x, y, z in mm (stage coordinates). z is updated after an autofocus."""

    def __init__(self, id, group, index, number, x, y, z):
        self.id = id              # the text typed in the positions table, e.g. 'wellA-3' ('' if none); NOT used in file names / csv ID columns (row number = index)
        self.group = group        # part of the ID before the first '-'  ('default' if the ID is empty)
        self.index = index        # 0-based position in the table (all points)
        self.number = number      # 0-based position within its group
        self.x, self.y, self.z = x, y, z
        self.z_initial = z
        self.table_xyz = (x, y, z)   # as read from the positions table (used to detect edits made while the job is paused)

    def __repr__(self):
        return f'Point(id={self.id!r}, group={self.group!r}, index={self.index}, x={self.x:.3f}, y={self.y:.3f}, z={self.z})'


class TimePoint:
    def __init__(self, index, start, late):
        self.index = index        # 0-based
        self.start = start        # seconds since the job started
        self.late = late          # seconds this time point started later than planned (0 = on time)

    def __repr__(self):
        return f'TimePoint(index={self.index}, start={self.start:.1f}s, late={self.late:.1f}s)'


def group_of(point_id):
    g = str(point_id).split('-')[0].strip()
    return g if g else 'default'


def make_points(ids, coords):
    """ids: list of str; coords: list of (x, y[, z]) in mm -> list of Point (GUI order)."""
    pts, per_group = [], {}
    for i, (pid, c) in enumerate(zip(ids, coords)):
        pid = '' if pid is None else str(pid)
        g = group_of(pid)
        n = per_group.get(g, 0)
        per_group[g] = n + 1
        z = float(c[2]) if len(c) > 2 else None
        pts.append(Point(pid, g, i, n, float(c[0]), float(c[1]), z))
    return pts


def resolve_channel_name(name, channel_names):
    """The channel a (part of a) name stands for: exact match, else the one channel that contains it (case-insensitive).
    Raises JobError for unknown or ambiguous names (used by Scope and by the job designer's checker)."""
    if not isinstance(name, str):
        raise JobError(f'channel must be a name (text), got {name!r}')
    if name in channel_names:
        return name
    low = name.lower()
    hits = [c for c in channel_names if low in c.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise JobError(f'unknown channel {name!r}. Available: {list(channel_names)}')
    raise JobError(f'channel {name!r} is ambiguous: {hits}. Use a longer part of the name.')


def _safe(s):
    return re.sub(r'[^A-Za-z0-9._+-]+', '_', str(s)).strip('_') or 'x'



def stack_positions(planes=None, step_um=None, span_um=None):
    """Z positions of a stack centred on 0: give any TWO of planes, step_um, span_um (all three are accepted if consistent).
    Returns (planes, step_um, [offsets in um, ascending]); span = (planes - 1) * step. Raises JobError for anything else."""
    given = [v is not None for v in (planes, step_um, span_um)]
    if sum(given) < 2 and not (planes == 1):
        raise JobError('z-stack: give two of planes, step_um and span_um (e.g. planes=11, step_um=1.0)')
    if planes is not None:
        if isinstance(planes, bool) or int(planes) != planes or planes < 1:
            raise JobError(f'z-stack: planes must be a whole number >= 1, got {planes!r}')
        planes = int(planes)
    for name, v in (('step_um', step_um), ('span_um', span_um)):
        if v is not None and (not np.isfinite(v) or v < 0 or (name == 'step_um' and v == 0 and planes != 1)):
            raise JobError(f'z-stack: {name} must be > 0, got {v!r}')
    if planes is None:
        if step_um is None or step_um <= 0:
            raise JobError('z-stack: step_um must be > 0')
        n = span_um / step_um
        if abs(n - round(n)) > 1e-6 * max(1.0, n):
            raise JobError(f'z-stack: span_um={span_um:g} is not a multiple of step_um={step_um:g}; the span must hold a whole number of steps '
                           f'(e.g. span_um={round(n) * step_um:g} gives {round(n) + 1} planes)')
        planes = int(round(n)) + 1
        step = float(step_um)
    elif step_um is not None:
        step = float(step_um)
        if span_um is not None and abs(span_um - (planes - 1) * step) > 1e-6 * max(1.0, span_um):
            raise JobError(f'z-stack: planes={planes}, step_um={step:g} and span_um={span_um:g} do not agree '
                           f'(span = (planes - 1) * step = {(planes - 1) * step:g}); give only two of them')
    elif span_um is not None:
        step = float(span_um) / (planes - 1) if planes > 1 else 0.0
    else:
        step = 0.0                       # planes == 1
    span = (planes - 1) * step
    if span > MAX_STACK_SPAN_UM:
        raise JobError(f'z-stack: span {span:g} um is larger than the allowed {MAX_STACK_SPAN_UM:g} um (units are micrometres)')
    offs = [(i - (planes - 1) / 2.0) * step for i in range(planes)]
    return planes, step, offs

# ------------------------------------------------------------------------------------------------
# backend interface (implemented by the hardware backend and by the dry-run backend)
# ------------------------------------------------------------------------------------------------
class Backend:
    def channels(self):                 # list of channel names
        raise NotImplementedError

    def channel_info(self, name):       # dict(exposure_ms, gain, intensity)
        return {}

    def move_to(self, x, y, z):         # absolute stage move in mm, returns when settled
        raise NotImplementedError

    def position(self):                 # (x, y, z) mm
        raise NotImplementedError

    def capture(self, channel):         # -> (image ndarray, info dict)
        raise NotImplementedError

    def autofocus(self, channel=None, focus_measure=None):  # -> z in mm after focusing
        raise NotImplementedError

    def move_z(self, z):                # absolute z move in mm (x and y stay), returns when settled
        x, y, _ = self.position()
        self.move_to(x, y, z)

    def channel_z_offset_um(self, name):   # z offset of a channel (um) that z-stacks add to the plane positions
        return 0.0

    def pixel_size_um(self):            # pixel size for the image metadata (None = unknown)
        return None

    def save_image(self, image, path, channel):
        raise NotImplementedError

    def fast_movie(self, channel, n_frames, fps, bits=None, roi=None, exposure_ms=None, gain=None):
        """Video-rate recording in one channel. -> dict(frames (n, Y, X), frame_ids, timestamps_s, fps_set, exposure_ms, gain, pixel_format,
        incomplete, timeouts, aborted, error, notes, restore_problems, x_mm, y_mm, z_mm)"""
        raise JobError('this microscope backend cannot record fast movies')

    def abort_requested(self):
        return False

    # --- pause / resume (optional; a backend that never pauses needs none of these)
    def pause_requested(self):          # True while the user wants the job paused
        return False

    def take_new_positions(self):       # -> list of (x, y, z) in table order (same length as the job's points) or None
        return None

    def on_pause(self):                 # called when the job has reached a safe point and is paused
        pass

    def on_resume(self):                # called just before the job continues
        pass

    def log(self, message):
        pass

    def cleanup(self):
        pass


# ------------------------------------------------------------------------------------------------
# the scope
# ------------------------------------------------------------------------------------------------
class Scope:
    """What a job script sees. See control/jobs/README.md for the full description."""

    def __init__(self, backend, points, experiment_dir=None, clock=None, save_images=True, max_events=None, record=False, observer=None, trace_file=None, save_as='multipage'):
        self._backend = backend
        self._points = list(points)
        self._dir = experiment_dir
        self._clock = clock or RealClock()
        self._save = save_images and experiment_dir is not None
        self._max_events = max_events
        self._events = 0
        self.events = [] if record else None     # plan of the job for the graphical preview (dry run)
        self._observer = observer                # live view: called with small dicts (see _notify); must never break the job
        self._trace_file = os.path.abspath(trace_file) if trace_file else None   # job file: recorded events get the line number of the call (job designer)
        self._trace_cache = {}
        self._t0 = self._clock.now()
        self._tp = None
        self._current_point = None
        self._paused_total = 0.0                 # seconds spent paused (job time does not count them)
        self._used_paths = set()
        self._save_as = self._check_save_as(save_as)   # default of the save_as argument of capture/zstack/movie (JOBs tab)
        self._deferred = []                      # merges of single files into multipage files, run while the camera is idle (see _run_deferred)
        self._visit = None                       # multipage captures of the current point and time point, written as ONE file (see _buffer_capture)
        self._image_counter = 0
        self._writer = None                      # background file writer (lossy / stack / movie files)
        self._csv = {}
        self._log_file = None
        self._channel_names = list(backend.channels())
        self.stats = dict(images=0, autofocus=0, moves=0, timepoints=0, late_timepoints=0, max_late_s=0.0,
                          per_channel={}, per_group={}, exposures={}, warnings=[], paused_s=0.0, stacks=0, movies=0)
        self._groups = {}
        for p in self._points:
            self._groups.setdefault(p.group, []).append(p)

    # --- information -------------------------------------------------------------------------
    @property
    def channels(self):
        """Names of the available channels (those of the channel configuration)."""
        return list(self._channel_names)

    @property
    def groups(self):
        """dict: group name -> list of Points (GUI order)."""
        return {g: list(p) for g, p in self._groups.items()}

    @property
    def all_points(self):
        return list(self._points)

    def points(self, *groups):
        """Points in GUI order. points() = all; points('A') or points('A', 'B') = those groups, in the GUI order."""
        if not groups:
            return list(self._points)
        names = []
        for g in groups:
            names.extend(g if isinstance(g, (list, tuple)) else [g])
        unknown = [g for g in names if g not in self._groups]
        if unknown:
            raise JobError(f'unknown group {unknown}; groups are: {list(self._groups)}')
        return [p for p in self._points if p.group in names]

    @property
    def elapsed(self):
        """Seconds since the job started."""
        return self._clock.now() - self._t0

    @property
    def experiment_dir(self):
        return self._dir

    @property
    def abort_requested(self):
        return bool(self._backend.abort_requested())

    def check_abort(self):
        if self._backend.abort_requested():
            raise JobAborted()

    # --- logging -----------------------------------------------------------------------------
    def log(self, message):
        line = f'[{self.elapsed:9.1f} s] {message}'
        self._backend.log(line)
        if self._dir is not None:
            if self._log_file is None:
                self._log_file = open(os.path.join(self._dir, 'job_log.txt'), 'a', encoding='utf-8')
            self._log_file.write(line + '\n')
            self._log_file.flush()

    def warn(self, message):
        self.stats['warnings'].append(message)
        self.log('WARNING: ' + message)

    def _notify(self, msg):
        """Tell the live view (if any) what is going on. Messages are plain dicts with 'type' and 't' (job time, s)."""
        if self._observer is not None:
            msg.setdefault('t', self.elapsed)
            try:
                self._observer(msg)
            except Exception:
                pass

    def _state(self, state, **kw):
        if self._observer is not None:
            self._notify(dict(type='state', state=state, tp=self._tp.index if self._tp is not None else None, **kw))

    def _rec(self, kind, t0, t1=None, **kw):
        if self.events is not None or self._observer is not None:
            pt = kw.pop('point', None)
            ev = dict(kind=kind, t0=t0, t1=self.elapsed if t1 is None else t1,
                      point=pt.index if pt is not None else None,
                      tp=self._tp.index if self._tp is not None else None, **kw)
            if self._trace_file is not None:
                line = self._job_line()
                if line is not None:
                    ev['line'] = line
            if self.events is not None:
                self.events.append(ev)
            self._notify(dict(type='done', **ev))

    def _job_line(self):
        """Line in the job file that is executing (innermost frame of the job file), None if not found."""
        f = sys._getframe(1)
        while f is not None:
            fn = f.f_code.co_filename
            hit = self._trace_cache.get(fn)
            if hit is None:
                hit = self._trace_cache[fn] = (os.path.abspath(fn) == self._trace_file)
            if hit:
                return f.f_lineno
            f = f.f_back
        return None

    def _tick(self, restore=True):
        self._drain_writer()
        self.check_abort()
        self._events += 1
        if self._max_events and self._events > self._max_events:
            raise DryRunLimit(self._max_events)
        if self._backend.pause_requested():
            self._pause_now(restore)

    # --- pause / resume ----------------------------------------------------------------------
    def _pause_now(self, restore=True):
        """Hold the job here (between two operations) until the backend says resume. Job time does not run while
        paused, so planned waits/intervals simply continue where they were. On resume the positions edited in the table
        meanwhile are taken over, and the stage returns to the current point."""
        self._flush_visit()
        self._run_deferred()
        t_p = self._clock.now()
        pt = self._current_point
        self._state('paused', point=pt.index if pt is not None else None, t=self.elapsed)
        self.log('job PAUSED')
        self._backend.on_pause()
        while self._backend.pause_requested():
            if self._backend.abort_requested():
                raise JobAborted()
            self._clock.sleep(0.1)
        self._t0 += self._clock.now() - t_p           # job time did not run while paused
        self._apply_positions(self._backend.take_new_positions())
        self._backend.on_resume()
        self._paused_total += self._clock.now() - t_p
        self.stats['paused_s'] = round(self._paused_total, 1)
        self.log(f'job RESUMED (paused for {self._clock.now() - t_p:.1f} s)')
        self._state('idle', reason='resumed')
        pt = self._current_point
        if restore and pt is not None:
            self._state('moving', point=pt.index)
            self._backend.move_to(pt.x, pt.y, pt.z)

    def _apply_positions(self, new):
        if new is None:
            return
        if len(new) != len(self._points):
            self.warn(f'the positions table has {len(new)} entries but the job started with {len(self._points)}; '
                      f'positions added or removed while paused are ignored')
            return
        changed = 0
        for p, c in zip(self._points, new):
            xyz = (float(c[0]), float(c[1]), float(c[2]) if len(c) > 2 and c[2] is not None else None)
            if xyz != p.table_xyz:
                p.x, p.y, p.z = xyz
                p.z_initial = xyz[2]
                p.table_xyz = xyz
                changed += 1
        if changed:
            self.log(f'{changed} position(s) were changed while the job was paused; the new coordinates are used from now on')

    def _sleep_until(self, target, **state_kw):
        """Wait until job time `target` (s); abort and pause stay responsive. Returns when the time has come."""
        self._flush_visit()                       # images taken so far are written before a (long) wait
        self._run_deferred()                      # the camera is idle now: merge single files into multipage files
        while True:
            self.check_abort()
            if self._backend.pause_requested():
                self._pause_now(True)
                self._state('waiting', until=target, **state_kw)     # the countdown continues with the same target
                continue
            if self.elapsed >= target:
                return
            stop = lambda: bool(self._backend.abort_requested() or self._backend.pause_requested())
            if self._clock.wait_until(self._t0 + target, stop):
                return

    # --- channels ----------------------------------------------------------------------------
    def _resolve_channel(self, name):
        return resolve_channel_name(name, self._channel_names)

    # --- time --------------------------------------------------------------------------------
    def wait(self, seconds):
        """Wait (the abort button still works)."""
        self._tick()
        if seconds < 0:
            raise JobError('wait(seconds): seconds must be >= 0')
        t0 = self.elapsed
        self._state('waiting', until=t0 + seconds, reason='wait')
        self._sleep_until(t0 + seconds, reason='wait')
        self._rec('wait', t0)

    def wait_until(self, elapsed_seconds):
        """Wait until `elapsed_seconds` have passed since the start of the job (returns at once if already later)."""
        self._tick()
        t0 = self.elapsed
        self._state('waiting', until=max(t0, elapsed_seconds), reason='wait_until')
        self._sleep_until(elapsed_seconds, reason='wait_until')
        self._rec('wait', t0)

    def timepoints(self, n, interval=None):
        """Loop over n time points:  for t in scope.timepoints(48, interval=1800): ...

        interval (s) is the time from the START of one time point to the START of the next. If a time point
        takes longer than the interval, the next one starts immediately (none is skipped), the delay is
        logged and the following interval counts from that start. n=None runs until aborted."""
        i, prev_start, planned = 0, None, None
        while n is None or i < n:
            self._tick()
            if i > 0 and interval:
                planned = prev_start + interval
                t_w0 = self.elapsed
                self._state('waiting', until=planned, reason='interval', next_tp=i)
                self._sleep_until(planned, reason='interval', next_tp=i)
                self._rec('wait', t_w0)
            start = self.elapsed
            late = max(0.0, start - planned) if planned is not None else 0.0
            tp = TimePoint(i, start, late)
            self._tp = tp
            self._rec('timepoint', start, start, late=late, planned=planned)
            self._notify(dict(type='timepoint', index=i, start=start, late=late, planned=planned, n=n, interval=interval,
                              next_start=(start + interval) if interval else None))
            self.stats['timepoints'] += 1
            if late > 0.5:
                self.stats['late_timepoints'] += 1
                self.stats['max_late_s'] = max(self.stats['max_late_s'], late)
                self.log(f'time point {i} started {late:.1f} s late (the previous one took longer than the interval)')
            else:
                self.log(f'time point {i} starts')
            yield tp
            prev_start = start
            i += 1

    # --- stage -------------------------------------------------------------------------------
    def move_to(self, point):
        """Move the stage to a Point (x, y, z; with all the usual backlash/safety handling)."""
        self._tick(restore=False)
        if isinstance(point, int) and not isinstance(point, bool):
            point = self._points[point]
        if not isinstance(point, Point):
            raise JobError(f'move_to needs a Point from scope.points(), got {point!r}')
        self._flush_visit()                       # the multipage file of the previous point is complete
        t0 = self.elapsed
        self._state('moving', point=point.index)
        self._backend.move_to(point.x, point.y, point.z)
        self._rec('move', t0, point=point)
        self._current_point = point
        self.stats['moves'] += 1

    # --- autofocus ---------------------------------------------------------------------------
    def autofocus(self, channel=None, focus_measure=None):
        """Contrast autofocus at the current position, with the settings of the Autofocus tab
        (focus measure, number of planes, step). channel: autofocus channel, default = the one of the Autofocus tab.
        focus_measure: e.g. "GLVA" or "LAPE", for this autofocus only (default = the one of the Autofocus tab, which stays unchanged).
        The focused z is remembered for the current point, so later visits start there. Returns z in mm."""
        self._tick()
        ch = self._resolve_channel(channel) if channel else None
        z_before = self._backend.position()[2]
        t_start = self.elapsed
        self._state('autofocus', point=self._current_point.index if self._current_point else None)
        z_after = self._backend.autofocus(ch) if focus_measure is None else self._backend.autofocus(ch, focus_measure=focus_measure)
        pt = self._current_point
        if pt is not None and pt.z_initial is not None and abs(z_after - pt.z_initial) > AF_MAX_Z_DRIFT_MM:
            self.warn(f'autofocus moved z by {z_after - pt.z_initial:.3f} mm at point {pt.index} ({pt.id}); '
                      f'more than {AF_MAX_Z_DRIFT_MM} mm - the new z is not stored for later visits')
        elif pt is not None:
            pt.z = z_after
        self.stats['autofocus'] += 1
        self._rec('autofocus', t_start, point=pt, channel=ch)
        self._write_row('autofocus', dict(
            time_s=round(t_start, 2), point_index=pt.index if pt else '', point_id=pt.index if pt else '',
            timepoint=self._tp.index if self._tp else '', channel=ch or '', z_before_mm=z_before, z_after_mm=z_after,
            duration_s=round(self.elapsed - t_start, 2), group=pt.group if pt else ''))
        return z_after

    # --- imaging -----------------------------------------------------------------------------
    def _compression(self, compression, quality, ratio=None):
        """Validate compression/quality/ratio BEFORE anything is acquired. -> ('lossless', None, None) or ('lossy', dB or None, ratio or None)."""
        if isinstance(ratio, dict):
            ratio = {self._resolve_channel(k): v for k, v in ratio.items()}      # short channel names are fine
        try:
            mode, q, ratio = stackio.normalize_compression(compression, quality, ratio)
            if mode == 'lossy':
                stackio.check_lossy_ok()
        except stackio.StackIOError as e:
            raise JobError(str(e))
        return mode, q, ratio

    def _drain_writer(self, final=False):
        """Log what the background writer has finished (lossy files: quality, achieved compression, codec error vs image noise)."""
        w = self._writer
        if w is None:
            return
        results, w.results = w.results, []
        for r in results:
            rel = os.path.relpath(r['file'], self._dir) if self._dir else r['file']
            if r.get('mode') == 'lossy':
                ch = [c for c in (r.get('channels') or []) if c.get('rmse') is not None]
                err = max([c['rmse'] for c in ch] or [0.0])
                noise = max([c['noise'] for c in ch] or [0.0])
                qtxt = ', '.join(f'{q:.0f}' for q in r.get('qualities', [r['quality_db']])) + ' dB'
                msg = (f'{rel}: lossy JPEG2000 {qtxt}, {r["ratio"]:.0f}x smaller than raw ({r["file_bytes"] / 1e6:.2f} MB)'
                       + (f'; codec error {err:.1f} counts rms vs image noise {noise:.1f} counts (middle plane)' if ch else ''))
                tg = r.get('targets') or []
                short = [f'{c.get("channel_index")}: {c["ratio"]:.1f}x of {t:g}x' for c, t in zip(r.get('channels') or [], tg) if t and c.get('ratio') is not None and c['ratio'] < 0.7 * t]
                if short:
                    self.warn(f'{rel}: the target compression was not reached in channel {"; ".join(short)} (lowest allowed quality is {stackio.QUALITY_RANGE_DB[0]:g} dB): '
                              f'the image has too much structure or noise for that ratio')
                self.log(msg)
                self._write_row('lossy_files', dict(file=rel, quality_db='/'.join(f'{q:.1f}' for q in r.get('qualities', [r['quality_db']])), target_ratio='/'.join(f'{t:g}' if t else '' for t in (r.get('targets') or [])),
                                                    achieved_ratio=round(r['ratio'], 1), file_mb=round(r['file_bytes'] / 1e6, 3),
                                                    codec_rmse_counts=round(err, 2), image_noise_counts=round(noise, 2)))

    def _get_writer(self):
        if self._writer is None:
            self._writer = stackio.AsyncWriter()
        return self._writer

    # --- file names ---------------------------------------------------------------------------
    # multipage (default):  <experiment>/Time0003_Point0002[_Zstack|_Movie].tiff   (all channels of the call in one file)
    # single files:         <experiment>/<time point>/<row number>_0_0_<k>_<channel>.tiff   (as the Flexible Multipoint tab; row number of the position in the table, 0-based; k = z plane)
    # a name that already exists gets the ending _ver_2, _ver_3, ...; filename=... replaces the whole name (without extension)
    _SAVE_AS = ('multipage', 'merge', 'single')

    @classmethod
    def _check_save_as(cls, value):
        if not isinstance(value, str) or value.strip().lower() not in cls._SAVE_AS:
            raise JobError(f'save_as must be "multipage" (written directly), "merge" (single files first, merged into the multipage file later) '
                           f'or "single" (single files, kept), got {value!r}')
        return value.strip().lower()

    def _use_save_as(self, save_as):
        return self._save_as if save_as is None else self._check_save_as(save_as)

    @staticmethod
    def _clean_filename(filename):
        if filename is None:
            return None
        if not isinstance(filename, str) or not filename.strip():
            raise JobError(f'filename must be a non-empty text without extension, e.g. "my_image", got {filename!r}')
        name = re.sub(r'(\.ome)?\.tiff?$', '', os.path.basename(filename.strip().replace('\\', '/')), flags=re.I)
        return _safe(name)

    def _stem(self, descriptor=None, filename=None):
        """Name of a multipage file: Time0003_Point0002[_<descriptor>] (or `filename`)"""
        if filename:
            return filename
        pt, tp = self._name_point(), self._tp
        return f'Time{tp.index if tp else 0:04d}_' + (f'Point{pt.index:04d}' if pt is not None else 'Stage') + (f'_{descriptor}' if descriptor else '')

    def _name_point(self):
        """the point used in file names: the current one; a job with exactly one point that never called move_to (Single measurement tab) uses that point"""
        if self._current_point is None and len(self._points) == 1:
            return self._points[0]
        return self._current_point

    def _region(self):
        pt = self._name_point()
        # as the standard (Flexible Multipoint) acquisition of the previous software: the typed ID never enters the file name,
        # the name of a position is its row number in the positions table (0-based), e.g. 3/17_0_0_0_<channel>.tiff
        return str(pt.index) if pt is not None else 'stage'

    def _new_path(self, base, ext='.tiff', single=False):
        """Unique file path: in the experiment folder (multipage) or in the folder of the time point (single files); _ver_N if the name exists."""
        folder = self._dir
        if single:
            folder = os.path.join(self._dir, str(self._tp.index) if self._tp else '0')
        os.makedirs(folder, exist_ok=True)
        path, n = os.path.join(folder, base + ext), 1
        while path in self._used_paths or os.path.exists(path):
            n += 1
            path = os.path.join(folder, f'{base}_ver_{n}{ext}')
        self._used_paths.add(path)
        return path

    def _buffer_capture(self, ch, image, info, t_cap, mode, q, ratio, filename, save_as='multipage', part=None):
        """Multipage / merge mode: the images of one point and time point (or of one `filename`) go into ONE file, written when the job moves on
        (next point, wait, time point end, pause, job end). multipage: the images are kept in memory; merge: `part` is the single file that was
        just written, and it is merged into the multipage file later (when the camera waits) and then deleted.
        The file name is reserved now. -> path relative to the experiment folder"""
        pt, tp = self._current_point, self._tp
        key = (tp.index if tp else None, pt.index if pt is not None else None, filename, mode, q, repr(ratio), save_as)
        v = self._visit
        if v is not None and (v['key'] != key or ch in [it['ch'] for it in v['items']]):
            self._flush_visit()
            v = None
        if v is None:
            v = self._visit = dict(key=key, mode=mode, q=q, ratio=ratio, save_as=save_as, path=self._new_path(self._stem(None, filename)), items=[])
        arr = np.asarray(image)
        v['items'].append(dict(ch=ch, image=np.array(image) if part is None else None, part=part, shape=arr.shape, dtype=arr.dtype, info=info, t=t_cap))
        return os.path.relpath(v['path'], self._dir)

    def _flush_visit(self):
        v, self._visit = self._visit, None
        if not v or not v['items']:
            return
        its = v['items']
        chans = [it['ch'] for it in its]
        planes = dict(time_s=[it['t'] - its[0]['t'] for it in its], exposure_ms=[it['info'].get('exposure_ms') or 0.0 for it in its],
                      x_um=[(it['info'].get('x_mm') or 0.0) * 1000 for it in its], y_um=[(it['info'].get('y_mm') or 0.0) * 1000 for it in its],
                      z_um=[(it['info'].get('z_mm') or 0.0) * 1000 for it in its])
        px, path, mode, q, ratio = self._backend.pixel_size_um(), v['path'], v['mode'], v['q'], v['ratio']
        desc = 'multipage capture: ' + ', '.join(chans) + (', lossy JPEG2000' if mode == 'lossy' else '')
        if v['save_as'] == 'merge':
            parts = {(0, 0, ci): it['part'] for ci, it in enumerate(its)}
            src = stackio.PageFiles(parts, (1, 1, len(its)), its[0]['dtype'], its[0]['shape'][-2:])
            self._defer(lambda: self._merge_job(path, src, chans, planes, px, None, None, mode, q, ratio, desc))
            return
        data = np.stack([it['image'] for it in its])[None, None]            # (T=1, Z=1, C, Y, X)
        self._get_writer().submit(
            lambda: stackio.write_ome_tiff(path, data, chans, planes=planes, pixel_size_um=px, compression=mode, quality=q, ratio=ratio, description=desc),
            int(data.nbytes), self._backend.abort_requested)

    # merge mode: the single files are merged into the multipage file in the writer thread, but only while the camera has nothing to do
    def _defer(self, fn):
        self._deferred.append(fn)
        if len(self._deferred) >= 200:            # never let single files pile up for ever (jobs without waits)
            self._run_deferred()

    def _run_deferred(self):
        jobs, self._deferred = self._deferred, []
        for fn in jobs:
            self._get_writer().submit(fn, 0)

    def _merge_job(self, path, src, chans, planes, px, z_step_um, interval_s, mode, q, ratio, desc):
        """(writer thread) multipage file from single files; they are deleted only if the new file has the right number of pages and no page was missing"""
        info = stackio.write_ome_tiff(path, src, chans, planes=planes, pixel_size_um=px, z_step_um=z_step_um, time_increment_s=interval_s,
                                      compression=mode, quality=q, ratio=ratio, description=desc)
        n_pages = int(np.prod(src.shape[:3]))
        if src.missing:
            raise stackio.StackIOError(f'{os.path.basename(path)}: {len(src.missing)} single file(s) were missing; the existing single files were kept')
        if not stackio.merge_ok(list(src.paths.values()), path, n_pages):
            raise stackio.StackIOError(f'{os.path.basename(path)}: the merged file could not be verified; the single files were kept')
        for p in src.paths.values():
            try:
                os.remove(p)
            except OSError:
                pass
        return info

    def _grab(self, ch):
        """One image through the backend, with the bookkeeping shared by capture, zstack and movie. -> (image, info, job time)"""
        t_cap = self.elapsed
        self._state('capturing', point=self._current_point.index if self._current_point else None, channel=ch)
        image, info = self._backend.capture(ch)
        info = dict(info or {})
        pt = self._current_point
        self._image_counter += 1
        self._rec('capture', t_cap, point=pt, channel=ch)
        self.stats['images'] += 1
        self.stats['per_channel'][ch] = self.stats['per_channel'].get(ch, 0) + 1
        if pt is not None:
            self.stats['per_group'][pt.group] = self.stats['per_group'].get(pt.group, 0) + 1
        if info.get('exposure_ms') is not None:
            self.stats['exposures'][ch] = info['exposure_ms']
        return image, info, t_cap

    def capture(self, channel, save=True, compression='lossless', quality=None, ratio=None, save_as=None, filename=None):
        """Take one image in `channel` at the current position and return it as a NumPy array.
        The image is saved automatically and listed in images.csv; save=False returns it only.
        save_as (default: the JOBs tab setting): "multipage" = the images of this point and time point go into ONE file Time0003_Point0002.tiff,
        written directly (when the job moves on); "merge" = each image is first written as a single file, and the single files are merged into
        the multipage file later (while the job waits) and deleted; "single" = one file per image <time point>/<ID>_0_0_0_<channel>.tiff.
        filename="my_name": use this name (no extension) instead of the automatic one. compression: "lossless" (default) or "lossy" (JPEG2000)."""
        self._tick()
        ch = self._resolve_channel(channel)
        mode, q, ratio = self._compression(compression, quality, ratio)
        sa, fname = self._use_save_as(save_as), self._clean_filename(filename)
        image, info, t_cap = self._grab(ch)
        pt, tp = self._current_point, self._tp
        rel = ''
        if save and self._save:
            if sa == 'multipage':
                rel = self._buffer_capture(ch, image, info, t_cap, mode, q, ratio, fname)
            elif sa == 'merge':
                part = self._new_path(f'{self._region()}_0_0_0_{_safe(ch)}', single=True)      # lossless single file, merged (and recompressed if lossy) later
                self._backend.save_image(image, part, ch)
                rel = self._buffer_capture(ch, image, info, t_cap, mode, q, ratio, fname, save_as='merge', part=part)
            else:
                path = self._new_path(fname or f'{self._region()}_0_0_0_{_safe(ch)}', single=True)
                if mode == 'lossless':
                    self._backend.save_image(image, path, ch)
                else:
                    data = np.array(image)[None, None, None]
                    planes = dict(time_s=[0.0], exposure_ms=[info.get('exposure_ms') or 0.0], x_um=[(info.get('x_mm') or 0.0) * 1000],
                                  y_um=[(info.get('y_mm') or 0.0) * 1000], z_um=[(info.get('z_mm') or 0.0) * 1000])
                    px = self._backend.pixel_size_um()
                    self._get_writer().submit(lambda: stackio.write_ome_tiff(path, data, [ch], planes=planes, pixel_size_um=px,
                                                                             compression='lossy', quality=q, ratio=ratio, description='lossy JPEG2000'),
                                              data.nbytes, self._backend.abort_requested)
                rel = os.path.relpath(path, self._dir)
        self._write_row('images', dict(
            image_id=self._image_counter, file=rel, time_s=round(t_cap, 2), wall_time=datetime.datetime.now().isoformat(timespec='milliseconds'),
            timepoint=tp.index if tp else '', point_index=pt.index if pt else '', point_id=pt.index if pt else '',
            group=pt.group if pt else '', channel=ch, x_mm=info.get('x_mm', ''), y_mm=info.get('y_mm', ''), z_mm=info.get('z_mm', ''),
            exposure_ms=info.get('exposure_ms', ''), gain=info.get('gain', ''), intensity=info.get('intensity', '')))
        return image

    def capture_channels(self, channels, save=True, compression='lossless', quality=None, ratio=None, save_as=None, filename=None):
        """capture() for several channels in the given order; returns {channel: image}. "multipage"/"merge": all channels in ONE file
        (Time0003_Point0002.tiff); "single": one file per channel (with filename: <filename>_<channel>)."""
        sa, fname = self._use_save_as(save_as), self._clean_filename(filename)
        out = {}
        for c in channels:
            ch = self._resolve_channel(c)
            out[ch] = self.capture(c, save=save, compression=compression, quality=quality, ratio=ratio, save_as=sa,
                                   filename=fname if (sa != 'single' or not fname) else f'{fname}_{_safe(ch)}')
        return out

    # --- z-stacks and movies -----------------------------------------------------------------
    def _resolve_channel_list(self, channels, what):
        names = [channels] if isinstance(channels, str) else list(channels)
        if not names:
            raise JobError(f'{what}: no channel given')
        chans = [self._resolve_channel(c) for c in names]
        if len(set(chans)) != len(chans):
            raise JobError(f'{what}: a channel is listed twice: {chans}')
        return chans

    def _move_z(self, z_mm, state, check=True):
        if state['z'] is not None and abs(state['z'] - z_mm) < 1e-6:
            return
        if check:
            self.check_abort()
        self._backend.move_z(z_mm)
        state['z'] = z_mm

    def _check_buffer(self, image, n_images, what):
        if np.ndim(image) != 2:
            raise JobError(f'{what}: only single-plane (grayscale) images are supported, got shape {np.shape(image)}')
        need = int(np.asarray(image).nbytes) * n_images
        if need > MAX_BUFFER_BYTES:
            raise JobError(f'{what}: {n_images} images of {np.asarray(image).nbytes / 1e6:.1f} MB need {need / 1e9:.1f} GB of memory, '
                           f'more than the allowed {MAX_BUFFER_BYTES / 1e9:.0f} GB. Use fewer planes/frames.')

    def _write_frames(self, descriptor, kind, data, chans, rows, px, z_step_um, interval_s, mode, q, ratio, description, save_as, filename, raw=False):
        """Write the (T,Z,C,Y,X) array in the background and add one row per page to frames.csv.
        save_as "multipage": ONE multipage file; "single": one file per image; "merge": one file per image first, merged into the multipage file
        later (when the job waits), then deleted. raw: no compression at all (fast movies). -> file path (multipage, merge) or folder (single files), relative to the experiment dir"""
        rel = ''
        cmode = 'none' if raw else mode
        if not self._save:
            return rel
        pt, tp = self._current_point, self._tp
        planes = dict(time_s=[r['dt_s'] for r in rows], exposure_ms=[r['exposure_ms'] or 0.0 for r in rows],
                      x_um=[(r['x_mm'] or 0.0) * 1000 for r in rows], y_um=[(r['y_mm'] or 0.0) * 1000 for r in rows],
                      z_um=[(r['z_mm'] or 0.0) * 1000 for r in rows])
        if save_as == 'multipage':
            path = self._new_path(self._stem(descriptor, filename))
            rel = os.path.relpath(path, self._dir)
            self._get_writer().submit(
                lambda: stackio.write_ome_tiff(path, data, chans, planes=planes, pixel_size_um=px, z_step_um=z_step_um,
                                               time_increment_s=interval_s, compression=cmode, quality=q, ratio=ratio, description=description),
                int(data.nbytes), self._backend.abort_requested)
            files = [rel] * len(rows)
        else:
            merge = save_as == 'merge'
            files, parts = [], {}
            for r in rows:
                ch = r['channel']
                if kind == 'zstack':
                    base = f'{filename}_Z{r["z_index"]:04d}_{_safe(ch)}' if (filename and not merge) else f'{self._region()}_0_0_{r["z_index"]}_{_safe(ch)}'
                else:
                    base = f'{filename}_F{r["t_index"]:04d}_{_safe(ch)}' if (filename and not merge) else f'{self._region()}_0_0_0_{_safe(ch)}_{descriptor}_F{r["t_index"]:04d}'
                spath = self._new_path(base, single=True)
                files.append(os.path.relpath(spath, self._dir))
                parts[(r['t_index'], r['z_index'], chans.index(ch))] = spath
                img = data[r['t_index'], r['z_index'], chans.index(ch)]
                page = dict(time_s=[0.0], exposure_ms=[r['exposure_ms'] or 0.0], x_um=[(r['x_mm'] or 0.0) * 1000], y_um=[(r['y_mm'] or 0.0) * 1000],
                            z_um=[(r['z_mm'] or 0.0) * 1000])
                if raw:                                  # uncompressed single page (fast movies)
                    self._get_writer().submit(
                        lambda img=img, spath=spath, ch=ch, page=page: stackio.write_ome_tiff(spath, img[None, None, None], [ch], planes=page, pixel_size_um=px,
                                                                                              compression='none', description=kind),
                        int(img.nbytes), self._backend.abort_requested)
                elif mode == 'lossless' or merge:       # merge: single files are always lossless; the merged file gets the requested compression
                    self._get_writer().submit(lambda img=img, spath=spath, ch=ch: self._backend.save_image(img, spath, ch), int(img.nbytes), self._backend.abort_requested)
                else:
                    self._get_writer().submit(
                        lambda img=img, spath=spath, ch=ch, page=page: stackio.write_ome_tiff(spath, img[None, None, None], [ch], planes=page, pixel_size_um=px,
                                                                                              compression='lossy', quality=q, ratio=ratio, description=kind),
                        int(img.nbytes), self._backend.abort_requested)
            if merge:
                path = self._new_path(self._stem(descriptor, filename))
                rel = os.path.relpath(path, self._dir)
                src = stackio.PageFiles(parts, data.shape[:3], data.dtype, data.shape[3:])
                self._defer(lambda: self._merge_job(path, src, chans, planes, px, z_step_um, interval_s, cmode, q, ratio, description))
                files = [rel] * len(rows)
            else:
                rel = os.path.relpath(os.path.dirname(spath), self._dir) + f' ({len(files)} files)' if files else ''
        for r, f in zip(rows, files):
            self._write_row('frames', dict(
                file=f, kind=kind, timepoint=tp.index if tp else '', point_index=pt.index if pt else '', point_id=pt.index if pt else '',
                group=pt.group if pt else '', **r))
        return rel

    def zstack(self, channels, planes=None, step_um=None, span_um=None, order='channel_major', compression='lossless', quality=None, ratio=None, save=True, save_as=None, filename=None):
        """Z-stack centred on the current z. Give two of planes, step_um, span_um. Returns {channel: array (planes, Y, X)}.
        order: "channel_major" = each channel scans the whole stack in turn; "z_major" = all channels at each plane, then the next plane.
        save_as (default: the JOBs tab setting): "multipage" = ONE multipage OME-TIFF per call, Time0003_Point0002_Zstack.tiff (axes Z, C), written directly;
        "merge" = single files first, merged into that file later (while the job waits); "single" = one file per image <time point>/<ID>_0_0_<plane>_<channel>.tiff.
        filename="name" replaces the automatic name (single files: name_Z0005_<channel>). compression: "lossless" (default) or "lossy" (JPEG2000, quality in dB)."""
        self._tick()
        chans = self._resolve_channel_list(channels, 'zstack')
        if order not in ('channel_major', 'z_major'):
            raise JobError(f'zstack: order must be "channel_major" or "z_major", got {order!r}')
        mode, q, ratio = self._compression(compression, quality, ratio)
        mp, fname = self._use_save_as(save_as), self._clean_filename(filename)
        n, step, offs = stack_positions(planes, step_um, span_um)
        x0, y0, z0 = self._backend.position()
        C = len(chans)
        offsets = {c: float(self._backend.channel_z_offset_um(c) or 0.0) for c in chans}
        zs = {c: [z0 + (o + offsets[c]) / 1000.0 for o in offs] for c in chans}
        data, rows = None, {}
        state = dict(z=z0)
        t_first = None
        t_begin = self.elapsed
        order_list = ([(i, ci) for ci in range(C) for i in range(n)] if order == 'channel_major'
                      else [(i, ci) for i in range(n) for ci in range(C)])
        try:
            for i, ci in order_list:
                ch = chans[ci]
                self.check_abort()
                self._events += 1
                if self._max_events and self._events > self._max_events:
                    raise DryRunLimit(self._max_events)
                self._move_z(zs[ch][i], state)
                image, info, t_cap = self._grab(ch)
                if data is None:
                    self._check_buffer(image, n * C, 'zstack')
                    data = np.zeros((1, n, C) + np.shape(image), dtype=np.asarray(image).dtype)
                    t_first = t_cap
                data[0, i, ci] = image
                rows[(i, ci)] = dict(frame=i * C + ci, t_index=0, z_index=i, channel=ch, time_s=round(t_cap, 3), dt_s=round(t_cap - t_first, 4),
                                     z_mm=info.get('z_mm', zs[ch][i]), x_mm=info.get('x_mm', x0), y_mm=info.get('y_mm', y0),
                                     exposure_ms=info.get('exposure_ms'), gain=info.get('gain', ''), intensity=info.get('intensity', ''))
        finally:
            try:
                self._move_z(z0, state, check=False)     # back to the centre (also after an error or abort)
            except BaseException:
                pass
        self.stats['stacks'] += 1
        # pages are written in (Z, C) order
        ordered = [rows[(i, ci)] for i in range(n) for ci in range(C)]
        desc = (f'z-stack: {n} planes, step {step:g} um, span {(n - 1) * step:g} um, centre z {z0 * 1000:.2f} um, order {order}, '
                f'channels {chans}, ' + ('lossless' if mode == 'lossless' else 'lossy JPEG2000'))
        rel = ''
        if save:
            rel = self._write_frames('Zstack', 'zstack', data, chans, ordered, self._backend.pixel_size_um(), step, None, mode, q, ratio, desc, mp, fname)
        self.log(f'z-stack: {n} planes x {C} channel(s), step {step:g} um, {self.elapsed - t_begin:.1f} s' + (f' -> {rel}' if rel else ''))
        return {c: data[0, :, ci] for ci, c in enumerate(chans)}

    def movie(self, channels, frames=None, interval_s=None, duration_s=None, compression='lossless', quality=None, ratio=None, save=True, save_as=None, filename=None):
        """Movie at the current position: `frames` rounds of imaging all `channels`, each round starting `interval_s` after the start
        of the previous one (as close as the hardware allows; interval_s=None = back to back). Give frames, or duration_s together with
        interval_s. Returns {channel: array (frames, Y, X)}. ONE multipage OME-TIFF per call (axes T, C) with the real time of every image:
        save_as (default: the JOBs tab setting): "multipage" = Time0003_Point0002_Movie.tiff written directly; "merge" = single files first, merged later;
        "single" = one file per image <time point>/<ID>_0_0_0_<channel>_Movie_F0012.tiff.
        filename="name" replaces the automatic name (single files: name_F0012_<channel>).
        The achieved intervals are logged and stored in the file and in frames.csv. Pause takes effect after the movie, Abort stops it."""
        self._tick()
        chans = self._resolve_channel_list(channels, 'movie')
        mode, q, ratio = self._compression(compression, quality, ratio)
        mp, fname = self._use_save_as(save_as), self._clean_filename(filename)
        if frames is not None and duration_s is not None:
            raise JobError('movie: give frames OR duration_s (with interval_s), not both')
        if interval_s is not None and (not np.isfinite(interval_s) or interval_s <= 0):
            raise JobError(f'movie: interval_s must be > 0, got {interval_s!r}')
        if duration_s is not None:
            if interval_s is None:
                raise JobError('movie: duration_s needs interval_s')
            frames = int(round(duration_s / interval_s))
        if frames is None:
            raise JobError('movie: give frames (or duration_s and interval_s)')
        if isinstance(frames, bool) or int(frames) != frames or frames < 1:
            raise JobError(f'movie: frames must be a whole number >= 1, got {frames!r}')
        frames = int(frames)
        C = len(chans)
        data, rows, starts = None, [], []
        t_begin = self.elapsed
        for n in range(frames):
            self.check_abort()
            self._events += 1
            if self._max_events and self._events > self._max_events:
                raise DryRunLimit(self._max_events)
            if interval_s is not None and n > 0:
                target = t_begin + n * interval_s
                self._state('waiting', until=target, reason='movie')
                if not self._clock.wait_until(self._t0 + target, lambda: bool(self._backend.abort_requested())):
                    raise JobAborted()
            starts.append(self.elapsed)
            for ci, ch in enumerate(chans):
                image, info, t_cap = self._grab(ch)
                if data is None:
                    self._check_buffer(image, frames * C, 'movie')
                    data = np.zeros((frames, 1, C) + np.shape(image), dtype=np.asarray(image).dtype)
                data[n, 0, ci] = image
                rows.append(dict(frame=n * C + ci, t_index=n, z_index=0, channel=ch, time_s=round(t_cap, 3), dt_s=round(t_cap - t_begin, 4),
                                 z_mm=info.get('z_mm', ''), x_mm=info.get('x_mm', ''), y_mm=info.get('y_mm', ''),
                                 exposure_ms=info.get('exposure_ms'), gain=info.get('gain', ''), intensity=info.get('intensity', '')))
        self.stats['movies'] += 1
        # timing report
        st = np.asarray(starts)
        d = np.diff(st) if len(st) > 1 else np.array([])
        report = f'movie: {frames} frame(s) x {C} channel(s)'
        if len(d):
            report += f', achieved interval {d.mean():.3f} s (sd {d.std():.3f}, min {d.min():.3f}, max {d.max():.3f})'
            if interval_s is not None:
                sched = t_begin + np.arange(frames) * interval_s
                dev = st - sched
                report += f' for requested {interval_s:g} s; start-time error vs schedule: max {np.abs(dev).max() * 1000:.0f} ms'
                if d.mean() > interval_s * 1.02 or (dev > max(0.05, 0.1 * interval_s)).any():
                    self.warn(f'movie could not keep the requested interval of {interval_s:g} s: achieved {d.mean():.3f} s on average '
                              f'(longest {d.max():.3f} s). One round takes about {np.median([e for e in np.diff(st)]):.2f} s here; use a longer interval or fewer channels.')
        self.log(report)
        desc = (report + f'. Channels {chans}, ' + ('lossless' if mode == 'lossless' else 'lossy JPEG2000') +
                '. Page time (DeltaT) = job time when the image was requested (start of the exposure +- readout).')
        rel = ''
        if save:
            rel = self._write_frames('Movie', 'movie', data, chans, rows, self._backend.pixel_size_um(), None,
                                     float(d.mean()) if len(d) else None, mode, q, ratio, desc, mp, fname)
            if rel:
                self.log(f'movie saved as {rel}')
        return {c: data[:, 0, ci] for ci, c in enumerate(chans)}

    def fast_movie(self, channel, fps=25, duration_s=None, frames=None, roi=None, bits=None, exposure_ms=None, gain=None,
                   compression='none', save=True, save_as=None, filename=None):
        """Video-rate movie at the current position in ONE channel: the camera runs freely at `fps` with the light on, the frames are collected in
        memory (no processing, no disk access while recording) and written afterwards. Returns {channel: array (frames, Y, X)}.
        Give duration_s or frames. roi: region in the centre of the sensor, a side length (2000 = 2000x2000 pixels) or (width, height);
        None = the current camera region. bits: 8 or 16 (None = the current setting); 8 bit halves data and readout time, but saturates at 255.
        exposure_ms / gain: only for this movie (None = the channel's values); the camera settings are restored afterwards.
        compression: "none" (default, uncompressed), "lossless" or "lossy". save_as, filename: as for movie().
        The camera frame number and clock of every frame are stored (fast_frames.csv, OME DeltaT); dropped frames, the achieved frame rate
        and saturation are reported. Pause takes effect after the movie, Abort stops it (the frames recorded so far are kept)."""
        self._tick()
        ch = self._resolve_channel(channel)
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not np.isfinite(fps) or fps <= 0:
            raise JobError(f'fast_movie: fps must be a number > 0, got {fps!r}')
        if (frames is None) == (duration_s is None):
            raise JobError('fast_movie: give frames OR duration_s')
        if duration_s is not None:
            if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool) or not np.isfinite(duration_s) or duration_s <= 0:
                raise JobError(f'fast_movie: duration_s must be a number > 0, got {duration_s!r}')
            frames = int(round(duration_s * fps))
        if isinstance(frames, bool) or int(frames) != frames or frames < 1:
            raise JobError(f'fast_movie: frames must be a whole number >= 1, got {frames!r}')
        frames = int(frames)
        if bits not in (None, 8, 16):
            raise JobError(f'fast_movie: bits must be 8 or 16, got {bits!r}')
        if roi is not None:
            side = [roi] if np.isscalar(roi) else list(roi)
            if len(side) not in (1, 2) or any(isinstance(s, bool) or int(s) != s or s < 16 for s in side):
                raise JobError(f'fast_movie: roi must be a side length or (width, height) in pixels (at least 16), got {roi!r}')
            roi = int(side[0]) if len(side) == 1 else (int(side[0]), int(side[1]))
        for name, v in (('exposure_ms', exposure_ms), ('gain', gain)):
            if v is not None and (isinstance(v, bool) or not np.isfinite(v) or v < 0 or (name == 'exposure_ms' and v == 0)):
                raise JobError(f'fast_movie: {name} must be a positive number, got {v!r}')
        comp = 'none' if compression is None else str(compression).strip().lower()
        if comp not in ('none', 'lossless', 'lossy'):
            raise JobError(f'fast_movie: compression must be "none", "lossless" or "lossy", got {compression!r}')
        mode, q, ratio = self._compression('lossless' if comp == 'none' else comp, None, None)
        mp, fname = self._use_save_as(save_as), self._clean_filename(filename)
        self.check_abort()
        self._events += 1
        if self._max_events and self._events > self._max_events:
            raise DryRunLimit(self._max_events)
        t_begin = self.elapsed
        self._state('capturing', point=self._current_point.index if self._current_point else None, channel=ch)
        res = self._backend.fast_movie(ch, frames, float(fps), bits=bits, roi=roi, exposure_ms=exposure_ms, gain=gain)
        data = res.get('frames')
        got = 0 if data is None else len(data)
        if got == 0:
            raise JobError(f'fast_movie: the camera delivered no frames ({res.get("error") or "unknown reason"})')
        pt = self._current_point
        self._rec('capture', t_begin, point=pt, channel=ch)
        self.stats['movies'] += 1
        self.stats['images'] += got
        self.stats['per_channel'][ch] = self.stats['per_channel'].get(ch, 0) + got
        if pt is not None:
            self.stats['per_group'][pt.group] = self.stats['per_group'].get(pt.group, 0) + got
        if res.get('exposure_ms') is not None:
            self.stats['exposures'][ch] = res['exposure_ms']
        # --- what happened: timing, lost frames, brightness
        ids = np.asarray(res.get('frame_ids') or [], dtype=np.int64)
        lost = int(np.clip(np.diff(ids) - 1, 0, None).sum()) if len(ids) > 1 else 0
        ts = np.asarray(res.get('timestamps_s') or [], dtype=float)
        have_ts = len(ts) == got and got > 1 and np.ptp(ts) > 0
        dt = (ts - ts[0]) if have_ts else np.arange(got) / float(fps)
        d = np.diff(ts) if have_ts else np.array([])
        achieved = float(1.0 / d.mean()) if len(d) else None
        problems = []
        if lost:
            problems.append(f'{lost} frame(s) lost (gaps in the camera frame numbers)')
        if res.get('incomplete'):
            problems.append(f'{res["incomplete"]} incomplete frame(s) skipped')
        if res.get('timeouts'):
            problems.append(f'{res["timeouts"]} timeout(s) waiting for the camera')
        if got < frames:
            problems.append(('aborted' if res.get('aborted') else 'stopped') + f' after {got} of {frames} frames' + (f' ({res["error"]})' if res.get('error') else ''))
        if achieved is not None and abs(achieved - fps) > 0.03 * fps:
            problems.append(f'achieved {achieved:.2f} fps instead of {fps:g}')
        if len(d) and d.max() > 1.5 / fps:
            problems.append(f'longest frame interval {d.max() * 1000:.0f} ms (nominal {1000 / fps:.0f} ms)')
        for p in res.get('restore_problems') or []:
            problems.append('camera setting not restored: ' + p)
        sample = np.asarray(data[np.linspace(0, got - 1, min(got, 5)).astype(int)])
        dmax = float(np.iinfo(sample.dtype).max) if sample.dtype.kind in 'ui' else 1.0
        levels = [dmax] + ([16383.0, 4095.0] if sample.dtype == np.uint16 else [])
        sat = max(float((sample == lv).mean()) for lv in levels)
        top = float(np.percentile(sample[:, ::8, ::8], 99.9))
        brightness = f'brightest 0.1% of pixels at {top:.0f} of {dmax:.0f}'
        if sat >= 0.001:
            self.warn(f'fast_movie {ch}: {sat * 100:.1f}% of the pixels are saturated. Reduce exposure_ms, gain or the illumination intensity and repeat.')
        elif sample.dtype == np.uint8 and top < 40:
            self.warn(f'fast_movie {ch}: the image is dark ({brightness}); 8-bit frames have only 256 grey levels, so use more light or exposure.')
        for p in problems:
            self.warn(f'fast_movie {ch}: {p}')
        for n in res.get('notes') or []:
            self.warn(f'fast_movie {ch}: {n}')
        h, w = data.shape[-2:]
        rep = (f'fast movie {ch}: {got} frames {w}x{h} {res.get("pixel_format") or str(data.dtype)}, {achieved:.2f} fps' if achieved else
               f'fast movie {ch}: {got} frames {w}x{h} {res.get("pixel_format") or str(data.dtype)}') + \
              f' (requested {fps:g}' + (f', camera set to {res["fps_set"]:.2f}' if res.get('fps_set') else '') + \
              f'), exposure {res.get("exposure_ms") if res.get("exposure_ms") is not None else "?"} ms, {brightness}, {lost} lost'
        self.log(rep)
        rel = ''
        if save:
            x, y, z = res.get('x_mm', ''), res.get('y_mm', ''), res.get('z_mm', '')
            rows = [dict(frame=i, t_index=i, z_index=0, channel=ch, time_s=round(t_begin + float(dt[i]), 4), dt_s=round(float(dt[i]), 5),
                         z_mm=z, x_mm=x, y_mm=y, exposure_ms=res.get('exposure_ms'), gain=res.get('gain', ''), intensity=res.get('intensity', ''))
                    for i in range(got)]
            desc = rep + '. DeltaT = camera clock since the first frame. ' + ('Uncompressed.' if comp == 'none' else ('Lossless.' if mode == 'lossless' else 'Lossy JPEG2000.'))
            if self._save:
                rel = self._write_frames('FastMovie', 'movie', np.asarray(data)[:, None, None], [ch], rows, self._backend.pixel_size_um(), None,
                                         (1.0 / achieved) if achieved else 1.0 / fps, mode, q, ratio, desc, mp, fname, raw=(comp == 'none'))
                for i in range(got):
                    self._write_row('fast_frames', dict(file=rel, frame=i, camera_frame_id=int(ids[i]) if i < len(ids) else '',
                                                        camera_time_s=round(float(dt[i]), 6), interval_ms=round(float(d[i - 1]) * 1000, 3) if (i and have_ts) else ''))
                if rel:
                    self.log(f'fast movie saved as {rel}')
        return {ch: data}

    # --- bookkeeping -------------------------------------------------------------------------
    def _write_row(self, table, row):
        if self._dir is None:
            return
        entry = self._csv.get(table)
        if entry is None:
            f = open(os.path.join(self._dir, table + '.csv'), 'w', newline='', encoding='utf-8')
            w = csv.DictWriter(f, fieldnames=list(row))
            w.writeheader()
            entry = self._csv[table] = (f, w)
        entry[1].writerow(row)
        entry[0].flush()

    def _close(self):
        try:
            self._flush_visit()
            self._run_deferred()
        except Exception as e:
            self.log(f'ERROR writing the last multipage file: {e!r}')
        if self._writer is not None:
            errs = self._writer.close()
            self._drain_writer(True)
            self._writer = None
            for e in errs:
                self.log(f'ERROR writing an image file: {e}')
        for f, _ in self._csv.values():
            f.close()
        self._csv = {}
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
