# JOBs: scripted acquisition

A **job** is a normal Python file that defines `run_job(scope)`. It says *what* is imaged and *when* (loops over
positions and time points, channels, autofocus, waiting). It never controls hardware directly: everything goes
through the high-level `scope` object, which uses the same tested movement, autofocus and imaging code as the
standard acquisition. The standard acquisition (Flexible Multipoint tab) is unchanged.

## Documenting a job (shown when the file is opened)

When you select a job file in the JOBs tab, the log (and the terminal) shows its documentation. Write it like this:

```python
"""One-line title of the job."""

# ---- settings: edit these ----
CHANNELS = ["BF LED matrix full", "488"]
N_TIMEPOINTS = 12

def run_job(scope):
    """WHAT IT DOES: ... (what is imaged, in which order, how often)

    YOU PROVIDE:
      - Positions: what has to be defined in the Flexible Multipoint tab (IDs / groups)
      - Settings at the top of the file, and what they mean
    """
    ...
```

Shown: the one-line title (module docstring), the docstring of `run_job`, and all UPPER_CASE settings of the file with their
current values. Files without a docstring get a reminder to add one. The three files in `job_examples/` follow this pattern.

## Live view while a job runs

The JOBs tab shows the state of a running job (updated twice per second; no matplotlib needed):

- a **status line**: what the scope is doing now (moving to / imaging position X channel Y / autofocus / waiting), with a
  **countdown** and clock time while it waits for the next time point (or in `scope.wait()`), and a thin bar that fills up during the wait,
- the **time point** that is running, how long it has been running, when the next one is planned (or that it will start immediately because the pass takes longer than the interval), time points that started late,
- **images done / planned** (from the check run) and an **estimated end** (planned duration plus the delay already accumulated),
- a row of boxes for **all time points** (done / now / waiting to start / upcoming, orange foot = started late) and a row of boxes for the **positions of the current time point** (imaged / now / pending, red mark = autofocus done).

"Planned" values come from the dry run that is done when you press *Start job*; a job whose number of images depends on image
content can differ. The view does not influence the job: if it ever fails, the job continues.

## Pausing a job

While a job runs, **Pause job** stops it at the next safe point (between two operations, e.g. after the image being taken;
a running move/exposure is never interrupted). The button then turns into **Resume job**. While paused:

* the stage controls and the **Flexible Multipoint** tab can be used: move the stage, re-focus, and change positions
  (e.g. "Offset positions", or editing a coordinate). Positions cannot be added or removed while paused (resume is refused
  if the count differs); changes to the "group" column are ignored until the next job.
* the other acquisition tabs and the Start button of Flexible Multipoint stay locked.
* **Job time stands still**: a planned wait or interval simply continues with the remaining time after resume (the
  countdown shows it), and time points keep their planned job-time schedule. Time spent paused is listed in the summary.

On resume the changed coordinates are used from then on, live view is switched off if it was on, and the stage goes back
to the current position (with the z-lift for safety) before the job continues exactly where it was.
Abort works while paused.

The live view shows a large countdown in seconds ("Next acquisition in 125 s") while the job waits.

## Checking a job graphically

*Show job plan (graph)* in the JOBs tab (or `python -m control.jobs.plot job.py positions.csv --channels BF,488 --out plan.png`
from the `software` folder) runs the dry run and draws what the job would do:

- a **timeline per position** (rows grouped by group; one tick per image, colour = channel; triangles = autofocus; dashed lines = time points, red = started late),
- a **detail** of the first time point (or any range you type in the window) showing the order of channels, autofocus and moves,
- a **stage map** coloured by group with the visiting order (red rings: positions the job never images),
- the **working time per time point** against the interval (shows when the interval is too short),
- a **matrix** of images per channel and autofocus runs for each time point (e.g. "BF only every 4th time point").

The graph needs the Python package `matplotlib` (`pip install matplotlib` in the environment that starts the software; everything else works without it).

Times are estimates (assumed duration of moves and autofocus plus the exposure times); use the graph to check order and
structure, not exact timing. If the job fails, the figure shows everything up to the error.

## Job designer (editor, test, graphic, help)

**JOBs tab -> "Job designer..."** opens an editor in which jobs are written and tested; it can also be started alone with
`python -m control.jobs_designer [job.py]` from the `software` folder. Nothing in the designer moves the stage or switches on light.

* **Editor:** Python colours, line numbers, Tab/Shift+Tab, Ctrl+/ (comment), Ctrl+F (find). Typing `scope.` lists all functions with a short
  description (Ctrl+Space anywhere); inside `scope.capture("` the channel names are offered, in `scope.points("` the groups, after `pt.` the attributes of a position.
  Hover over a function for its description, press **F1** on a name for the full help.
* **Live check while typing** (`control/jobs/lint.py`): syntax errors, typos (`scope.captur`, `pt.indx`), wrong arguments, unknown channel names
  (also in `CHANNELS = [...]` at the top), unknown groups, a loop over positions without `move_to`, `time.sleep` (cannot be aborted), undefined names.
  Problems are underlined, marked in the margin and listed in the *Problems* tab.
* **Test (F5):** runs the job in a dry run (simulated microscope and time, so a 2-day job takes a moment; a job stuck in an endless loop is stopped after 30 s). The dry run stops after 200000 scope operations (`DRY_RUN_MAX_EVENTS` in `control/jobs/runner.py`); a longer job (e.g. a lifespan experiment with many positions) is then reported as *only partly tested* with a warning, but it is **not refused** and can be started; a real run has no such limit.
  *Output* shows the summary, warnings, `print()` output and `scope.log()` messages. If the job fails, the line is marked in the editor.
  *Step list* lists every operation with time and line; double click jumps to the line.
* **Plan graphic** (no matplotlib needed): timeline per position (ticks = images coloured by channel, triangles/boxes = autofocus, grey = moves,
  dashed = time points, red = late), stage map (colour = group, order of visits, positions never imaged), working time per time point against the interval,
  and an images-per-time-point matrix. Mouse wheel zooms time, drag moves, double click = whole job, hover = details, click an image = jump to the code line.
* **Test setup:** the positions (IDs define the groups) and channels (exposure, gain, intensity) used for the test, and the assumed duration of a move,
  an image and an autofocus. Positions and channels are taken from the Flexible Multipoint tab / channel configuration when the designer is opened from the
  JOBs tab; they can be edited, replaced by sample positions or loaded from a CSV. Edits there affect only the test.
* **Help panel:** all functions (search, description, arguments, example, *Insert call* / *Insert example*), guide pages (quick start, positions and groups,
  time points, channels, autofocus, pause/abort, files, *Python in 2 minutes*), the examples (*Open a copy in the editor*) and ready-made snippets.
  The function reference is `control/jobs/apidoc.py`; `tests/test_jobs_designer.py` checks that every `scope` function is documented there and that the
  documented signatures match the code.
* **Use in JOBs tab** (Ctrl+U) saves the job and selects it in the JOBs tab (checked); the job is started there. It is refused while a job is running.

## Workflow

1. **Flexible Multipoint tab:** define the positions (stage position + optional z). Give them **IDs** in the
   positions table; the **group** of a position is the part of its ID before the first `-`
   (`A-0`, `A-1`, `B-0` ... -> groups `A` and `B`; an empty ID belongs to the group `default`).
2. **JOBs tab:** *Select job...* (or *New from example...*), choose saving folder and experiment ID,
   *Check job (dry run)*, then *Start job*. *Abort job* stops it safely (illumination off).
3. The positions are read from the Flexible Multipoint tab when you press Check/Start.

*Check job* runs your script with simulated time and no hardware. It finds errors (unknown channel, typos), counts
images per channel and group, autofocus runs, and estimates the total duration and late time points. Jobs
that decide on image *content* may behave differently in the dry run (images are dummy arrays).

## The `scope` API

```python
def run_job(scope):
    for t in scope.timepoints(48, interval=1800):      # 30 min from start to start
        for pt in scope.points("A", "B"):              # points of groups A and B, in table order
            scope.move_to(pt)
            if (t.index + pt.index) % 5 == 0:
                scope.autofocus()
            img = scope.capture("488")                 # NumPy array
```

| | |
|---|---|
| `scope.points()` / `scope.points("A", "B")` | list of `Point` in table order (all, or only those groups) |
| `scope.groups` | dict group name -> list of points |
| `Point` | `.id`, `.group`, `.index` (0-based in the table), `.number` (0-based within its group), `.x .y .z` (mm; z is updated after an autofocus) |
| `scope.timepoints(n, interval=None)` | loop; yields `TimePoint` with `.index`, `.start` (s since job start), `.late` (s). `n=None`: until aborted |
| `scope.move_to(point)` | move the stage (same backlash/safety handling as the standard acquisition) |
| `scope.autofocus(channel=None)` | contrast autofocus with the settings of the Autofocus tab; stores the focused z in the point; returns z (mm) |
| `scope.capture("488")` | one image in a channel; saved automatically (`save_as=`, `filename=`: see file names below); returns the image as NumPy array. `save=False`: do not save. `compression="lossy"` (default: error = 1x image noise per channel) or `compression="lossy", quality=80`: JPEG2000 (see below) |
| `scope.capture_channels(["BF LED matrix full", "488"])` | several channels in that order; returns `{channel: image}` |
| `scope.zstack(channels, planes=, step_um=, span_um=, order=, compression=, quality=, ratio=, save_as=, filename=)` | z-stack centred on the current z; give two of planes/step_um/span_um; one multipage OME-TIFF; returns `{channel: (planes, Y, X)}` |
| `scope.movie(channels, frames=, interval_s=, duration_s=, compression=, quality=, ratio=, save_as=, filename=)` | fast time series at the current position, one multipage OME-TIFF with the real time of every image; returns `{channel: (frames, Y, X)}` |
| `scope.fast_movie(channel, fps=25, duration_s=, frames=, roi=, bits=, exposure_ms=, gain=, compression="none", save_as=, filename=)` | video-rate movie in one channel (camera free running, frames in memory, written afterwards, uncompressed by default); the camera settings are restored; returns `{channel: (frames, Y, X)}` |
| `scope.wait(seconds)` | wait (abortable) |
| `scope.wait_until(seconds)` | wait until this many seconds after the job start |
| `scope.elapsed` | seconds since the job start |
| `scope.channels` | available channel names |
| `scope.log(text)`, `scope.warn(text)` | message in the JOBs tab and in `job_log.txt` |
| `scope.abort_requested` | True after the user pressed Abort (API calls stop the job anyway) |

**Channels** are named as in the channel configuration. Any unique part of the name, case-insensitive, works
(`"488"` for `Fluorescence 488 nm Ex`); ambiguous or unknown names are errors that list the choices.
Exposure, gain and intensity come from the channel configuration.

**Time points:** `interval` is the time from the *start* of one time point to the *start* of the next. If a time
point takes longer than the interval, the next one starts **immediately** (none is skipped), the delay is logged
and the following interval counts from that start.

**Abort:** *Abort job* raises an exception inside the job at the next `scope` call. It is not caught by
`except Exception:` in your script. Afterwards the illumination is off and the files are complete up to that point.

**Errors in the job:** the runner reports them (with the line in your file), turns the illumination off and
ends the experiment cleanly. Errors found by the dry run prevent the start.

## Z-stacks, movies and compression

* **Z-stack** (`scope.zstack`): centred on the *current* z (after `move_to`, and after `autofocus` the focused z). Two of `planes`, `step_um`,
  `span_um` (span = (planes - 1) x step). The stage scans upwards and returns to the centre. `order="channel_major"` (default): each channel
  scans the whole stack; `"z_major"`: all channels at each plane, then the next plane. The channel's z offset (channel configuration) is added.
  All planes and channels of one call are ONE file `Time0003_Point0002_Zstack.tiff` (OME axes Z, C); with `save_as="single"` one file per image (see file names below).
* **Movie** (`scope.movie`): rounds of all `channels`, each starting `interval_s` after the start of the previous (scheduled against a fixed
  clock; `interval_s=None` = back to back). ONE file per call (`Time0003_Point0002_Movie.tiff`, axes T, C; `save_as="single"`: one file per image). The real time of every image is in the OME
  metadata (`DeltaT`) and in `frames.csv`; the achieved interval (mean, sd, min, max) is logged and written into the file description. If the
  hardware cannot keep the interval a warning is issued. Time points of the job are separate files, as before. Pause takes effect after the movie.
* **Fast movie** (`scope.fast_movie`): video-rate recording (e.g. 25 fps) in ONE channel. The camera runs freely at `fps` (FLIR cameras), the light is on only
  while it records, frames are collected in memory (nothing is processed or written during the recording) and written afterwards, **uncompressed** by default
  (`Time0003_Point0002_FastMovie.tiff`). `roi=2000` = 2000x2000 pixels in the sensor centre; `bits=8` or `16`; `exposure_ms`/`gain` only for this movie. All
  camera settings are restored afterwards (also after errors). Camera frame number and clock of every frame go to `fast_frames.csv` and the OME `DeltaT`; lost
  frames, the achieved frame rate, saturation and too dark 8-bit images are reported as warnings. Limit: 4 GB in memory (2000x2000 at 8 bit: 40 s at 25 fps).
  Check on the microscope computer that `cat /sys/module/usbcore/parameters/usbfs_memory_mb` is large (1000); the log notes if it is not.
* **Saving:** `compression="lossless"` (default; zlib-compressed TIFF, bit-identical) or `"lossy"` (16-bit JPEG2000 inside the same TIFF).
  Lossy by default uses a **noise-relative quality**: per channel, shortly before the file is written (background thread), the pixel noise is estimated
  from the middle image (every 4th row/column, ~0.1 s) and the JPEG2000 quality is set so that the codec error is about **1x that noise**
  (`stackio.DEFAULT_NOISE_FACTOR`). For stacks and movies this is checked by encoding/decoding once to 1-3 times (~1 s each on a full frame, once per file);
  for single images only the formula is used (error ~ 65535 / 10^(dB/20) counts). Dim noisy channels are thus kept more precisely than bright ones.
  `quality=<dB>` (40 to 120; 60 dB = 65 counts error, 80 dB = 6.5 counts) sets a fixed quality instead. The compression depends on the image.
  Alternatively `ratio=` asks for a size: a number for all channels (`ratio=30`) or `ratio={"488": 50}` for single channels (others use the noise-relative default);
  the quality is then calibrated for every channel on its middle plane (whole plane, 1 dB steps) shortly before the file is written (background thread).
  The achieved ratio is at least the target, often far more (the codec's rate control jumps once the quality falls below the noise).
  Give only one of `quality` and `ratio`. Lossy removes camera noise and the finest detail: check on your own images that this is acceptable
  for your analysis; every lossy file is reported in `job_log.txt` and `lossy_files.csv`. Lossless stays the default.
  JPEG2000 encoding takes about 0.7 to 1.4 s per 2048 x 3088 frame; it runs in a background thread. Lossy needs `pip install imagecodecs`
  (a missing package is reported by the check and before anything is acquired).
* **Reading lossy files:** `tifffile` + `imagecodecs` (Python, napari via its tifffile reader), Bio-Formats (Fiji). Stacks and movies are held in
  memory until written (limit 4 GB).
* OME `PhysicalSizeX/Y` is the camera sensor pixel size from the configuration (as in the standard saving), not corrected for magnification.

## What is written to the experiment folder

| file | content |
|---|---|
| `Time0000_Point0000.tiff`, ... | **multipage / merge** (*Save as* in the JOBs tab, default "multipage TIFF, written directly"; `save_as="multipage"`, `"merge"` or `"single"` per call; with "merge" every image is first written as a single file and merged into this file later, while the job waits or at the end, then deleted): ONE file per point and time point with all images taken there (channels as pages; `capture`, `capture_channels`), `..._Zstack.tiff`, `..._Movie.tiff`; flat in the experiment folder. Images outside a time-point loop use `Time0000` |
| `0/`, `1/`, ... | **single files** (*Save as* single files): folder per time point (`0/` outside `scope.timepoints`), `<row number>_0_0_<z plane>_<channel>.tiff` as in the Flexible Multipoint tab (row number = 0-based position in the positions table; the typed group/ID is not part of the name); movie frames `<ID>_0_0_0_<channel>_Movie_F0012.tiff` |
| `_ver_2`, `_ver_3` | appended when a name already exists (e.g. the same channel captured twice at a point). `filename="my_name"` (no extension) replaces the automatic name in `capture`, `capture_channels`, `zstack`, `movie`; single files then get `my_name_<channel>`, `my_name_Z0005_<channel>`, `my_name_F0012_<channel>` |
| `lossy_files.csv` | one row per lossy file: quality, target and achieved compression, codec error and image noise (counts) |
| `fast_frames.csv` | one row per frame of a fast movie: file, frame, camera frame number, camera time, interval to the previous frame |
| `frames.csv` | one row per image of a z-stack or movie: file, kind, frame/z/t index, channel, job time, time since the start of the stack/movie, stage x/y/z, exposure, gain, intensity |
| `images.csv` | one row per single image (`capture`): file, time since start, wall time, time point, point index, point ID (= the row number, same as the index), group (as typed), channel, actual stage x/y/z, exposure, gain, intensity |
| `autofocus.csv` | one row per autofocus: time, point index, point ID (= row number), time point, channel, z before/after, duration, group (last column) |
| `job.py` | copy of the job file that was run |
| `job_log.txt` | what happened and when (incl. late time points, warnings) |
| `job_meta.json` | status (completed/aborted/failed), duration, counts, warnings, software commit |
| `positions_initial.csv`, `positions_final.csv` | positions before the job and with the z values after autofocus (columns: index, id (= row number), group (as typed), number_in_group, x, y, z) |
| `configurations.xml`, `acquisition parameters.json` | as in the standard acquisition |

## Limits of this first version

* XY points with several channels, autofocus, scheduling, z-stacks, movies. No tiled scans (NX x NY), no movie of z-stacks (T x Z);
  RGB and USB-spectrometer channels are not available.
* **Known issue:** with channels of *different exposure times* the camera may use the previous channel's exposure
  after a channel switch (see `tests/diagnose_exposure.py` on the `squid-improvements` branch). Until it is resolved,
  use the same exposure for all channels of a job; the check reports a warning otherwise.
* No napari / tiled-preview updates during a job; the live image display is updated.
* Single images are saved synchronously (as in the standard acquisition); lossy files, z-stacks and movies are written in a background thread.
* The job file is arbitrary Python, so it can do anything Python can; `scope` is the only supported way to control the microscope.

Examples: `software/job_examples/`.  Tests: `tests/test_jobs_api.py` (no hardware), `tests/e2e_jobs.py` (simulation or real microscope).
