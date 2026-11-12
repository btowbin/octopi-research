"""Hourly time lapse of 12 groups (405, 488, BF, 561): staggered autofocus on the main groups, subgroups (1a, 2a, ...) follow their main group."""
import statistics

# ---- settings: edit these ------------------------------------------------------------------
CHANNELS = ["405", "488", "BF LED matrix full", "561"]   # imaged in this order at every position (same exposure for all four!)
AF_CHANNEL = "561"                                # channel used for the autofocus
AF_FOCUS_MEASURE = "focus_measure_normalized_variance.py::normalized_variance"   # normalized variance (file in the software folder)
AF_EVERY = 3                                      # after the first time point every main position is refocused every 3rd time point (staggered)
MAIN_GROUPS = [str(i) for i in range(1, 13)]      # main groups "1" ... "12" (IDs 1-0, 1-1, ...); visited in this order
SUBGROUP_SUFFIX = "a"                             # subgroup of main group "1" is "1a" (IDs 1a-0, 1a-1, ...), of "2" is "2a", ...
N_TIMEPOINTS = 1440                                 # placeholder: set the number of hours
INTERVAL_S = 3600                                 # 1 h from the START of one time point to the START of the next
SAVE_AS = "single"                                # one file per image (no multipage / merged TIFFs): <time point>/<ID>_0_0_0_<channel>.tiff
COMPRESSION = "lossless"                          # lossless zlib TIFF
WARN_SHIFT_UM = 50                                # warn if the focus shift applied to a subgroup is larger than this (um)


def _median_shift_mm(main_points):
    """Median over ALL positions of the main group of (last focused z - z of the positions table), in mm, and the number of positions used.
    Positions that were focused at an earlier time point (staggered) count with their last result."""
    shifts = [p.z - p.z_initial for p in main_points if p.z is not None and p.z_initial is not None]
    return (statistics.median(shifts) if shifts else 0.0), len(shifts)


def run_job(scope):
    """WHAT IT DOES: every INTERVAL_S (1 h) all positions of the main groups 1-12 and of their subgroups 1a-12a are imaged in
    405, 488, BF and 561 (in this order, at the same exposure, no extra frame in between). The groups are visited one after the other:
    first the main group, then its subgroup.
    Main group positions (IDs 1-0, 1-1, ...): autofocus (channel 561, normalized variance) BEFORE the images
      - at the FIRST time point at ALL positions, and
      - afterwards staggered: the position with number k within its group is refocused at the time points t >= 1 where (t + k) is a
        multiple of AF_EVERY (3), i.e. every 3rd time point and about 1/3 of the positions of each group per time point.
        The focused z is stored for every position (as in the other jobs).
    Subgroup positions (IDs 1a-0, 1a-1, ...): NEVER autofocus. Before the group is imaged the focus shift of the main group is computed:
    for every position of the main group (also those that were last focused 1 or 2 time points ago) shift = last focused z - z of the
    positions table; the MEDIAN of these shifts is added to the table z of every subgroup position (z = table z + median shift).
    Saving: one lossless TIFF per image (no multipage, no merging).

    YOU PROVIDE:
      - Positions in the Flexible Multipoint tab with IDs 1-0, 1-1, ... 12-n for the main groups and 1a-0, 1a-1, ... for the subgroups
        (group = part of the ID before the first "-"). A subgroup needs its main group; groups that are missing are reported.
      - A z in the table for every position, recorded at about the same focus state (the subgroup keeps its offset to the main group
        as it was in the table, plus the measured shift).
      - The same exposure for 405, 488, BF and 561, and "Empty frame in between" (Camera tab) OFF; otherwise the start is refused or
        an extra frame is taken after each exposure change.
      - Number of planes and step of the autofocus: Autofocus tab (channel and focus measure are set by this job).
      - Check that one pass (about 12 x (60-90 + subgroup) positions x 4 images) fits into 1 h (Show job plan: working time per time point).
    """
    groups = scope.groups                         # {group: [Point, ...]}; the Point objects are shared, z changes are seen
    known = set(MAIN_GROUPS) | {g + SUBGROUP_SUFFIX for g in MAIN_GROUPS}
    for g in MAIN_GROUPS:
        if g not in groups:
            scope.warn(f'main group {g} is not in the positions table: skipped')
        elif g + SUBGROUP_SUFFIX not in groups:
            scope.log(f'group {g} has no subgroup {g + SUBGROUP_SUFFIX}')
    for g in groups:
        if g not in known:
            scope.warn(f'group {g} is neither a main group nor a subgroup: not imaged')
        elif g not in MAIN_GROUPS and g[:-len(SUBGROUP_SUFFIX)] not in groups:
            scope.warn(f'subgroup {g} has no main group {g[:-len(SUBGROUP_SUFFIX)]}: it is imaged without a focus shift')

    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for g in MAIN_GROUPS:
            main = groups.get(g, [])
            for pt in main:
                scope.move_to(pt)
                if t.index == 0 or (t.index + pt.number) % AF_EVERY == 0:      # first time point: all; later: staggered
                    scope.autofocus(channel=AF_CHANNEL, focus_measure=AF_FOCUS_MEASURE)
                scope.capture_channels(CHANNELS, save_as=SAVE_AS, compression=COMPRESSION)

            sub = groups.get(g + SUBGROUP_SUFFIX, [])
            if not sub:
                continue
            shift_mm, n = _median_shift_mm(main)           # all main positions, not only those focused now
            scope.log(f'time point {t.index}: subgroup {g + SUBGROUP_SUFFIX} follows group {g}: median focus shift {shift_mm * 1000:+.1f} um (of {n} positions)')
            if abs(shift_mm) * 1000 > WARN_SHIFT_UM:
                scope.warn(f'subgroup {g + SUBGROUP_SUFFIX}: focus shift {shift_mm * 1000:+.1f} um is larger than {WARN_SHIFT_UM} um')
            for pt in sub:
                if pt.z_initial is not None:
                    pt.z = pt.z_initial + shift_mm                              # table z + shift of the main group (no autofocus here)
                scope.move_to(pt)
                scope.capture_channels(CHANNELS, save_as=SAVE_AS, compression=COMPRESSION)
