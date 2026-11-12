"""Logic test of job_examples/08_twelve_groups_hourly_staggered_af_subgroups.py (simulated microscope, no hardware, no Qt).

Run from the software folder:   python tests/test_job_08_subgroups.py
Checks: AF at all main positions at t=0, then staggered every 3rd time point (by the number within the group), never at subgroup positions;
subgroup z = table z + MEDIAN over ALL main positions of (last focused z - table z) (also those focused 1-2 time points earlier);
channel order 405, 488, BF, 561 at every position; one single file per image (no multipage), file names; full-size dry run.
"""
import os
import re
import sys
import random
import shutil
import statistics
import tempfile

import numpy as np

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOFTWARE_DIR)
from control.jobs.api import make_points, SimClock, Backend          # noqa: E402
from control.jobs.runner import run_job, dry_run                      # noqa: E402

JOB = os.path.join(SOFTWARE_DIR, 'job_examples', '08_twelve_groups_hourly_staggered_af_subgroups.py')
CHANNELS = {'BF LED matrix full': dict(exposure_ms=10.0, gain=0.0, intensity=20.0),
            'Fluorescence 405 nm Ex': dict(exposure_ms=10.0, gain=0.0, intensity=30.0),
            'Fluorescence 488 nm Ex': dict(exposure_ms=10.0, gain=0.0, intensity=50.0),
            'Fluorescence 561 nm Ex': dict(exposure_ms=10.0, gain=0.0, intensity=50.0)}
EXPECTED_ORDER = ['Fluorescence 405 nm Ex', 'Fluorescence 488 nm Ex', 'BF LED matrix full', 'Fluorescence 561 nm Ex']
N_T = 7
INTERVAL = 3600
failures = []


def check(cond, msg):
    print(('PASS  ' if cond else 'FAIL  ') + msg)
    if not cond:
        failures.append(msg)


def drift_mm(g, t):                       # focus drift of main group g (1..12) after t hours (um -> mm)
    return (g - 6) * 0.8 * t / 1000.0


class SimBackend(Backend):
    def __init__(self, clock, truth_noise):
        self.clock, self.noise = clock, truth_noise
        self.pos = (0, 0, 0)
        self.table = {}            # (x, y) -> (id, group, number, table_z)
        self.af, self.caps = [], []
        self.paths = []

    def channels(self): return list(CHANNELS)
    def channel_info(self, name): return dict(CHANNELS[name])
    def position(self): return self.pos
    def move_to(self, x, y, z): self.clock.sleep(0.5); self.pos = (x, y, z)
    def _t(self): return int(self.clock.now() // INTERVAL)

    def capture(self, channel):
        self.clock.sleep(0.3)
        pid, _g, _n, _z = self.table[self.pos[:2]]
        self.caps.append((self._t(), pid, channel, self.pos[2]))
        info = dict(CHANNELS[channel]); info.update(x_mm=self.pos[0], y_mm=self.pos[1], z_mm=self.pos[2])
        return np.zeros((4, 4), dtype=np.uint16), info

    def autofocus(self, channel=None, focus_measure=None):
        self.clock.sleep(3)
        pid, g, n, tz = self.table[self.pos[:2]]
        self.af.append((self._t(), pid, channel, focus_measure))
        main = int(re.match(r'\d+', g).group())
        z = tz + drift_mm(main, self._t()) + self.noise[pid]            # what a perfect autofocus finds
        self.pos = (self.pos[0], self.pos[1], z)
        return z

    def save_image(self, image, path, channel):
        self.paths.append(path)
        open(path, 'wb').write(b'x')


def build(n_main, n_sub, no_sub=(), seed=1):
    rnd = random.Random(seed)
    ids, coords = [], []
    noise, tz = {}, {}
    k = 0
    for g in range(1, 13):
        for i in range(n_main(g)):
            pid = f'{g}-{i}'
            z = 1.0 + rnd.uniform(-0.02, 0.02)
            ids.append(pid); coords.append((10.0 + k * 0.01, 20.0 + g, z)); noise[pid] = rnd.uniform(-0.002, 0.002); tz[pid] = z; k += 1
        if g in no_sub:
            continue
        for i in range(n_sub(g)):
            pid = f'{g}a-{i}'
            z = 1.0 + rnd.uniform(-0.02, 0.02)
            ids.append(pid); coords.append((10.0 + k * 0.01, 20.0 + g, z)); noise[pid] = rnd.uniform(-0.002, 0.002); tz[pid] = z; k += 1
    return ids, coords, noise, tz


def main():
    tmp = tempfile.mkdtemp(prefix='job08_')
    try:
        job = os.path.join(tmp, 'job.py')
        src = open(JOB).read()
        src, n = re.subn(r'^N_TIMEPOINTS = \d+', f'N_TIMEPOINTS = {N_T}', src, flags=re.M)
        assert n == 1
        open(job, 'w').write(src)

        # ---- logic test: 12 groups, reduced sizes, group 7 without subgroup ----
        ids, coords, noise, tz = build(lambda g: 6 + g, lambda g: 4 + g // 2, no_sub=(7,))
        pts = make_points(ids, coords)
        clock = SimClock()
        be = SimBackend(clock, noise)
        for p in pts:
            be.table[(p.x, p.y)] = (p.id, p.group, p.number, p.z)
        exp_dir = os.path.join(tmp, 'exp'); os.makedirs(exp_dir)
        res = run_job(job, pts, be, experiment_dir=exp_dir, clock=clock, save_as='single')
        check(res.status == 'completed', f'job completed (status {res.status}, error {res.error})')
        check(not any('late' in m for m in be.__dict__.get('messages', [])), 'no late time points')

        main_pts = [p for p in pts if re.fullmatch(r'\d+', p.group)]
        sub_pts = [p for p in pts if p.group.endswith('a')]
        # AF schedule
        af_pairs = {(t, pid) for t, pid, _c, _m in be.af}
        check(len(af_pairs) == len(be.af), 'no autofocus twice at the same position and time point')
        check(all(not pid.split('-')[0].endswith('a') for _t, pid, _c, _m in be.af), 'no autofocus at any subgroup position')
        sched_ok = True
        for p in main_pts:
            for t in range(N_T):
                want = t == 0 or (t + p.number) % 3 == 0
                if want != ((t, p.id) in af_pairs):
                    sched_ok = False
        check(sched_ok, 'autofocus: all main positions at t=0, afterwards (t + number in group) % 3 == 0')
        check(all(c == 'Fluorescence 561 nm Ex' for _t, _p, c, _m in be.af), 'autofocus channel 561')
        check(all(m == 'focus_measure_normalized_variance.py::normalized_variance' for _t, _p, _c, m in be.af), 'autofocus focus measure = normalized variance')
        per_t = [sum(1 for t, _p, _c, _m in be.af if t == tt) for tt in range(N_T)]
        print('      AF per time point:', per_t, ' main positions:', len(main_pts))
        check(per_t[0] == len(main_pts), 'first time point: AF at all main positions')

        # subgroup z = table z + median of (last focused z - table z) over all main positions of the group
        by_pid = {p.id: p for p in pts}
        z_seen = {}
        for t, pid, ch, z in be.caps:
            z_seen.setdefault((t, pid), set()).add(round(z, 12))
        bad, checked = 0, 0
        for g in range(1, 13):
            mains = [p for p in main_pts if p.group == str(g)]
            subs = [p for p in sub_pts if p.group == f'{g}a']
            for t in range(N_T):
                shifts = []
                for p in mains:
                    t_last = max(tt for tt in range(t + 1) if tt == 0 or (tt + p.number) % 3 == 0)
                    shifts.append(drift_mm(g, t_last) + noise[p.id])
                med = statistics.median(shifts)
                for s in subs:
                    got = z_seen.get((t, s.id))
                    want = tz[s.id] + med
                    checked += 1
                    if not got or len(got) != 1 or abs(next(iter(got)) - want) > 1e-9:
                        bad += 1
        check(bad == 0, f'subgroup z = table z + median shift of ALL main positions (incl. stale ones), {checked} subgroup visits checked')
        # the shift really uses stale values (differs from a median over only the positions focused in this round)
        g, t = 12, 4
        mains = [p for p in main_pts if p.group == str(g)]
        fresh = [drift_mm(g, t) + noise[p.id] for p in mains if (t + p.number) % 3 == 0]
        allm = [drift_mm(g, max(tt for tt in range(t + 1) if tt == 0 or (tt + p.number) % 3 == 0)) + noise[p.id] for p in mains]
        check(abs(statistics.median(fresh) - statistics.median(allm)) > 1e-6, 'test is sensitive: "only this round" would give a different shift than "all positions"')

        # subgroup follows drift of its own main group (not another group)
        z_last = {pid: next(iter(v)) for (t, pid), v in z_seen.items() if t == N_T - 1}
        s12 = [p for p in sub_pts if p.group == '12a'][0]
        check(abs((z_last[s12.id] - tz[s12.id]) - drift_mm(12, N_T - 1)) < 0.004, 'subgroup 12a moved with group 12 (about +%.1f um)' % (drift_mm(12, N_T - 1) * 1000))

        # imaging: all positions, all time points, order of channels
        by_visit = {}
        for t, pid, ch, z in be.caps:
            by_visit.setdefault((t, pid), []).append(ch)
        check(len(by_visit) == len(pts) * N_T, f'every position imaged at every time point ({len(by_visit)} visits)')
        check(all(v == EXPECTED_ORDER for v in by_visit.values()), 'channel order 405, 488, BF, 561 at every position (no extra images)')
        # group 7 (no subgroup) and order: main group g, then its subgroup, then g + 1
        order = []
        for t, pid, ch, z in be.caps:
            if t == 0 and ch == EXPECTED_ORDER[0]:
                order.append(pid.split('-')[0])
        seq = [g for i, g in enumerate(order) if i == 0 or g != order[i - 1]]
        want_seq = []
        for g in range(1, 13):
            want_seq.append(str(g))
            if g != 7:
                want_seq.append(f'{g}a')
        check(seq == want_seq, 'visiting order: 1, 1a, 2, 2a, ..., 7, 8, 8a, ...')

        # files: single, lossless path, no multipage files
        n_img = len(pts) * N_T * 4
        files = []
        for d, _s, fs in os.walk(exp_dir):
            for f in fs:
                if f.endswith('.tiff'):
                    files.append(os.path.relpath(os.path.join(d, f), exp_dir))
        check(len(files) == n_img, f'{n_img} single image files written (got {len(files)})')
        check(not any(re.fullmatch(r'Time\d+_Point\d+.*\.tiff', f) for f in files), 'no multipage / merged files (Time####_Point####.tiff)')
        check(all(re.fullmatch(r'\d+[/\\]\d+_0_0_0_[A-Za-z0-9_]+\.tiff', f) for f in files), 'names <time point>/<row number>_0_0_0_<channel>.tiff, e.g. ' + sorted(files)[0])
        check(not any(f.endswith('.part') for f in files), 'no leftover merge parts')
        job_log = open(os.path.join(exp_dir, 'job_log.txt')).read()
        check('follows group 7:' not in job_log and 'group 7 has no subgroup 7a' in job_log, 'group without subgroup: only noted in the log')
        check('subgroup 12a follows group 12' in job_log, 'shift per subgroup and time point is logged')

        # ---- warnings for a wrong table ----
        ids2 = ['1-0', '1-1', '1a-0', '2a-0', '5-0', 'x-0']
        pts2 = make_points(ids2, [(1 + i, 1, 1.0) for i in range(6)])
        clock2 = SimClock(); be2 = SimBackend(clock2, {i: 0.0 for i in ids2})
        for p in pts2:
            be2.table[(p.x, p.y)] = (p.id, p.group, p.number, p.z)
        res2 = run_job(job, pts2, be2, clock=clock2, save_images=False)
        w = ' | '.join(res2.warnings + [m for m in be2.__dict__.get('messages', [])])
        check(res2.status == 'completed', 'job with missing groups still completes')
        check('main group 3 is not in the positions table' in ' '.join(res2.warnings) and 'group x is neither' in ' '.join(res2.warnings)
              and 'subgroup 2a has no main group 2' in ' '.join(res2.warnings), 'warnings: missing main group, unknown group, subgroup without main group')

        # ---- full size dry run: 12 groups x 60-90 main + subgroup positions; timing estimate ----
        ids3, coords3, _n3, _t3 = build(lambda g: 60 + (g * 7) % 31, lambda g: 20 + (g * 5) % 31, seed=3)
        pts3 = make_points(ids3, coords3)
        job_full = os.path.join(tmp, 'job_full.py')
        open(job_full, 'w').write(re.sub(r'^N_TIMEPOINTS = \d+', 'N_TIMEPOINTS = 4', open(JOB).read(), flags=re.M))
        res3 = dry_run(job_full, pts3, CHANNELS, record=False, move_s=0.5, autofocus_s=3.0, capture_overhead_s=0.2)
        main3 = sum(1 for p in pts3 if re.fullmatch(r'\d+', p.group))
        print(f'      full size: {len(pts3)} positions ({main3} main), status {res3.status}, estimated {res3.duration_s:.0f} s for 4 time points, stats: '
              f'images {res3.stats.get("captures", res3.stats.get("images", "?"))}, AF {res3.stats["autofocus"]}')
        check(res3.status == 'completed', 'full size dry run completes')
        check(res3.stats['autofocus'] == main3 + sum(1 for t in (1, 2, 3) for p in pts3 if re.fullmatch(r'\d+', p.group) and (t + p.number) % 3 == 0), 'full size: number of autofocus runs as expected')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%d failure(s)' % len(failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
