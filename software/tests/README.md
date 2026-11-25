# Tests

Run from the `software` folder with the venv's Python (`C:\Users\towbi\squid-sim\Scripts\python.exe` on the dev PC).

| Test | Needs | What it checks | Time |
|---|---|---|---|
| `tests/test_features_offscreen.py` | no hardware, no display | AF settings/schedule, camera ROI widget, Positions window (offset, undo, next/previous, move-to-selected), saving-dir warning | seconds |
| `tests/test_roi_centring.py` | fake PySpin with real Spinnaker ROI limits, no Qt | camera ROI always centred and software crops equal to the hardware ROI: many random changes, a failed write in the middle, drift repaired before an acquisition, fast movie leaves the ROI centred, camera without `get_ROI` unchanged | seconds |
| `tests/test_schedule_and_files.py` | no hardware | the expectations used by the e2e test agree with the code | seconds |
| `tests/test_jobs_api.py` | no hardware, no Qt | JOBs scripting core: scheduling (late time points), staggered autofocus, groups, files/metadata, abort and cleanup, error reporting, dry run, plan recording and graph | seconds |
| `tests/test_jobs_fast.py` | no hardware, no Qt | `scope.fast_movie`: file names and formats (uncompressed multipage / single / merged), `fast_frames.csv`, warnings (lost frames, saturation, dark 8-bit), validation, abort, dry run, lint | seconds |
| `tests/test_flir_fast.py` | fake PySpin | `Camera.fast_acquire`: recording at a fixed rate, ROI/pixel format/trigger mode/exposure/gain/frame rate restored after recording, after errors and after abort | seconds |
| `tests/test_hardware_backend_fast.py` | fake PySpin | `HardwareBackend.fast_movie` with the real Camera class: light on only while recording, camera restored, file and display | seconds |
| `tests/test_single_measurement_offscreen.py` | no hardware, offscreen Qt | the Single measurement tab: job it writes, checks before start, start/finish, JOBs tab ignores its messages | seconds |
| `tests/hardware_buffer_check.py` | **real camera + the disk to test** (no stage movement, no light) | large images with several exposures: lost/incomplete/stale frames and USB stream counters with no saving, single files, multipage, threaded writing; run once per disk (HDD, SSD) | ~5 min |
| `tests/hardware_fast_check.py` | **real camera** (no stage movement, no light) | highest frame rate at 8/16 bit for 2000 and 1000 px, a 5 s recording at 25 fps (lost frames, interval statistics), settings restored, `usbfs_memory_mb` | ~1 min |
| `tests/e2e_jobs.py --simulation` | simulation GUI (opens a window) | the JOBs tab with the real worker: job run, abort, refused broken jobs, job plan graph, standard acquisition unaffected | ~3 min |
| `tests/e2e_acquisition.py --simulation` | simulation GUI (opens a window) | real acquisitions: AF schedule per (time point, position), files on disk, multichannel | minutes |
| `tests/e2e_acquisition.py --hardware` | **real microscope, moves the stage** | same as above on the real system | minutes |

`python -m pytest tests` runs the first two (needs `pip install pytest`).

## End-to-end test

    python tests\e2e_acquisition.py --simulation --save-dir C:\temp\squid_e2e
    python tests\e2e_acquisition.py --simulation --save-dir C:\temp\squid_e2e --scenarios af_staggered --n-positions 8 --start-x 10 --start-y 10 --start-z 3
    python tests\e2e_acquisition.py --hardware --save-dir D:\test_runs --start-x 20 --start-y 20 --start-z 3.1 --spacing-mm 0.5 --n-positions 3

* `--save-dir` (required): an existing folder; every scenario creates its own experiment folder in it, plus `e2e_report_<time>.txt`.
* `--start-x/--start-y/--start-z` (mm): where the first position is. Default: current stage position. Positions are start + i*spacing along +x. The stage returns to the start at the end.
* Scenarios: af_every_timepoint, af_every_n, af_staggered, af_staggered_no_t0, af_off, multichannel (`--scenarios a,b`).
* `--hardware` shows the planned stage range and asks you to type MOVE (`--yes` skips); refuses spans above `--max-span-mm` (20).
* Needs `ome-types` and `tifffile` in the venv (image saving).
* Exit code 0 = all checks passed.
