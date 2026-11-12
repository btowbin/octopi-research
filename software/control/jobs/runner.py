"""Loads and runs a job file; owns error handling, cleanup and the reproducibility files."""
import os
import sys
import json
import shutil
import datetime
import importlib.util
import traceback
import subprocess
from dataclasses import dataclass, field

from control.jobs.api import Scope, JobError, JobAborted, DryRunLimit, SimClock, RealClock, make_points
from control.jobs.dryrun import DryRunBackend

# Limit of the DRY RUN only (check / plan / test), a real run has no limit (max_events=None). The dry run uses
# simulated time, so a job with an endless loop would never end; it is stopped after this many scope operations
# (a move, capture, autofocus, wait, ... counts as one). A job that reaches the limit is NOT refused: the dry run
# reports "only partly tested" (JobResult.incomplete + a warning) and the job can still be started.
DRY_RUN_MAX_EVENTS = 200000


@dataclass
class JobResult:
    status: str = 'not run'            # 'completed' | 'aborted' | 'failed' | 'invalid'
    error: str = ''
    duration_s: float = 0.0
    stats: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    events: list = field(default_factory=list)      # planned operations (dry run with record=True), see plot.py
    error_line: int = None                          # line of the job file where the problem is (None if unknown); used by the job designer
    incomplete: bool = False                        # dry run only: stopped at DRY_RUN_MAX_EVENTS, the rest of the job was not tested (status stays 'completed')


def _load_module(path):
    if not os.path.isfile(path):
        raise JobError(f'job file not found: {path}')
    name = 'squid_job_' + str(abs(hash(os.path.abspath(path))))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SyntaxError as e:
        err = JobError(f'syntax error in {os.path.basename(path)}, line {e.lineno}: {e.msg}')
        err.line = e.lineno
        raise err
    except Exception as e:
        err = JobError(f'error while loading {os.path.basename(path)}: {e!r}\n' + _user_traceback(path))
        err.line = _user_line(path)
        raise err
    return module


def load_job(path):
    """Import the job file and return its run_job function."""
    module = _load_module(path)
    fn = getattr(module, 'run_job', None)
    if not callable(fn):
        raise JobError(f'{os.path.basename(path)} must define  def run_job(scope):')
    return fn


def job_documentation(path):
    """Text shown when a job file is opened in the JOBs tab: the docstring of run_job (what the job does and what the
    user has to provide), the one-line title (module docstring) and the settings (UPPER_CASE names) of the file."""
    import inspect
    name = os.path.basename(path)
    try:
        module = _load_module(path)
    except JobError as e:
        return f'=== {name} ===\nThe job file could not be read:\n{e}'
    fn = getattr(module, 'run_job', None)
    lines = [f'=== {name} ===']
    title = inspect.getdoc(module)
    if title:
        lines.append(title)
    if not callable(fn):
        lines.append('WARNING: the file does not define  def run_job(scope):')
    else:
        doc = inspect.getdoc(fn)
        if doc:
            lines.append('')
            lines.append(doc)
        else:
            lines.append('')
            lines.append('(no description: add a docstring to run_job(scope) that says what the job does and what the user has to provide)')
    settings = [(k, v) for k, v in vars(module).items()
                if k.isupper() and not k.startswith('_') and not callable(v) and not inspect.ismodule(v)]
    if settings:
        lines.append('')
        lines.append('Settings in this file (edit the file to change them):')
        for k, v in settings:
            r = repr(v)
            lines.append(f'  {k} = {r if len(r) <= 100 else r[:97] + "..."}')
    return '\n'.join(lines)


def _user_traceback(path):
    """Traceback lines that belong to the job file (the useful part for the user)."""
    tb = traceback.extract_tb(sys.exc_info()[2])
    mine = [f'  {os.path.basename(f.filename)}, line {f.lineno}: {f.line}' for f in tb if os.path.abspath(f.filename) == os.path.abspath(path)]
    return '\n'.join(mine)


def _user_line(path):
    """Line of the job file in the innermost frame of the current exception (None if the job file is not involved)."""
    tb = traceback.extract_tb(sys.exc_info()[2])
    mine = [f.lineno for f in tb if os.path.abspath(f.filename) == os.path.abspath(path)]
    return mine[-1] if mine else None


def _git_commit():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=here, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def _exposure_warning(exposures):
    vals = {round(v, 3) for v in exposures.values() if v is not None}
    if len(vals) > 1:
        return ('the used channels have different exposure times (' + ', '.join(f'{c}: {v} ms' for c, v in exposures.items()) +
                '). Known issue: after a channel switch the camera may still use the previous channel\'s exposure; '
                'use the same exposure for all channels of a job until this is resolved.')
    return None


def run_job(job_path, points, backend, experiment_dir=None, clock=None, save_images=True, max_events=None, meta=None, record=False, observer=None, trace_file=None, save_as='multipage'):
    """Run the job file with the given backend. Never raises for problems in the job: returns a JobResult.
    points: list of Point (see api.make_points)."""
    res = JobResult()
    clock = clock or RealClock()
    scope = None
    started = datetime.datetime.now()
    try:
        fn = load_job(job_path)
        scope = Scope(backend, points, experiment_dir=experiment_dir, clock=clock, save_images=save_images, max_events=max_events, record=record, observer=observer, trace_file=trace_file, save_as=save_as)
        if experiment_dir is not None:
            shutil.copy2(job_path, os.path.join(experiment_dir, 'job.py'))
            _write_positions(os.path.join(experiment_dir, 'positions_initial.csv'), points)
            _write_meta(experiment_dir, meta, job_path, started, 'running', scope)
        scope.log(f'job {os.path.basename(job_path)} starts: {len(points)} points in {len(scope.groups)} group(s), channels: {scope.channels}; saving: ' + {'multipage': 'multipage TIFF files (written directly)', 'merge': 'single files merged into multipage TIFF files later', 'single': 'single TIFF files'}.get(save_as, save_as))
        try:
            fn(scope)
            res.status = 'completed'
        except JobAborted:
            res.status = 'aborted'
            scope.log('job aborted by the user')
        except DryRunLimit as e:
            # not an error: the dry run only checks the beginning of a very long job (a real run has no limit)
            res.status, res.incomplete = 'completed', True
            res.warnings.append(
                f'The dry run was stopped after {e.n_operations} operations (the limit of the test; a real run has no limit). '
                f'Only the first {scope.elapsed / 3600.0:.1f} h of the job (time point {scope.stats.get("timepoints", 0)}) could be tested: '
                'the rest of the job could not be tested, and the duration and image numbers shown cover only that part. '
                'A job with an endless loop would look the same.')
        except JobError as e:
            res.status, res.error = 'failed', str(e)
            res.error_line = _user_line(job_path)
        except Exception as e:
            res.status, res.error = 'failed', f'{e!r}\n' + _user_traceback(job_path)
            res.error_line = _user_line(job_path)
            scope.log('job FAILED: ' + res.error)
    except JobError as e:
        res.status, res.error = 'invalid', str(e)
        res.error_line = getattr(e, 'line', None)
    except BaseException as e:   # a bug in the runner itself: still clean up, then report
        res.status, res.error = 'failed', f'internal error: {e!r}\n{traceback.format_exc()}'
    finally:
        try:
            backend.cleanup()
        except Exception as e:
            res.warnings.append(f'cleanup problem: {e!r}')
        if scope is not None:
            res.duration_s = scope.elapsed
            res.stats = dict(scope.stats)
            res.events = scope.events or []
            res.warnings.extend(scope.stats['warnings'])
            w = _exposure_warning(scope.stats['exposures'])
            if w:
                res.warnings.append(w)
            if experiment_dir is not None:
                try:
                    scope.log(f'job finished: {res.status}' + (f' ({res.error.splitlines()[0]})' if res.error else ''))
                    _write_positions(os.path.join(experiment_dir, 'positions_final.csv'), points)
                    _write_meta(experiment_dir, meta, job_path, started, res.status, scope, res)
                except Exception as e:
                    res.warnings.append(f'could not write metadata: {e!r}')
            scope._close()
        if observer is not None:
            try:
                observer(dict(type='finished', status=res.status, error=res.error, t=res.duration_s))
            except Exception:
                pass
    return res


def dry_run(job_path, points, channel_infos, record=False, **backend_kwargs):
    """Run the job against simulated time to validate it and estimate what it does."""
    clock = SimClock()
    backend = DryRunBackend(channel_infos, clock=clock, **backend_kwargs)
    return run_job(job_path, points, backend, experiment_dir=None, clock=clock, save_images=False, max_events=DRY_RUN_MAX_EVENTS, record=record)


def describe(result):
    """Human readable summary of a (dry-run) result."""
    s = result.stats or {}
    lines = [f'status: {result.status}' + (' (only partly tested, see the warning below)' if getattr(result, 'incomplete', False) else '')]
    if result.error:
        lines.append('error: ' + result.error)
    if s:
        h = result.duration_s / 3600.0
        lines.append(f'images: {s.get("images", 0)}   ' + ', '.join(f'{c}: {n}' for c, n in s.get('per_channel', {}).items()))
        if s.get('per_group'):
            lines.append('per group: ' + ', '.join(f'{g}: {n}' for g, n in s['per_group'].items()))
        lines.append(f'autofocus runs: {s.get("autofocus", 0)}   moves: {s.get("moves", 0)}   time points: {s.get("timepoints", 0)}')
        lines.append(f'duration: {result.duration_s / 60:.1f} min ({h:.2f} h)' + (' (only the part that was tested)' if getattr(result, 'incomplete', False) else ''))
        if s.get('paused_s'):
            lines.append(f'time spent paused: {s["paused_s"]:.0f} s (not counted in the job time)')
        if s.get('late_timepoints'):
            lines.append(f'time points later than planned: {s["late_timepoints"]} (max {s["max_late_s"]:.0f} s) - the interval is shorter than one pass takes')
    for w in result.warnings:
        lines.append('warning: ' + w)
    return '\n'.join(lines)


def _write_positions(path, points):
    import csv
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['index', 'id', 'group', 'number_in_group', 'x_mm', 'y_mm', 'z_mm'])
        for p in points:
            w.writerow([p.index, p.index, p.group, p.number, p.x, p.y, p.z])    # id column = row number (as point_index), group = what was typed


def _write_meta(experiment_dir, meta, job_path, started, status, scope, result=None):
    d = dict(job_file=os.path.basename(job_path), started=started.isoformat(timespec='seconds'), status=status,
             software_commit=_git_commit(), channels=scope.channels, n_points=len(scope.all_points), groups=list(scope.groups))
    d.update(meta or {})
    if result is not None:
        d.update(finished=datetime.datetime.now().isoformat(timespec='seconds'), duration_s=round(result.duration_s, 1),
                 error=result.error, warnings=result.warnings, stats={k: v for k, v in result.stats.items() if k != 'warnings'})
    with open(os.path.join(experiment_dir, 'job_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=1, default=str)
