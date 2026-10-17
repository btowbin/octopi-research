# Tests

Run from the `software` folder with the venv's Python (`C:\Users\towbi\squid-sim\Scripts\python.exe` on the dev PC).

| Test | Needs | What it checks | Time |
|---|---|---|---|
| `tests/test_features_offscreen.py` | no hardware, no display | AF settings/schedule, camera ROI widget, Positions window (offset, undo, next/previous, move-to-selected), saving-dir warning | seconds |
| `tests/test_schedule_and_files.py` | no hardware | the expectations used by the e2e test agree with the code | seconds |
| `tests/test_diagnose_exposure_logic.py` | no hardware, no Qt | analysis logic of the exposure diagnostic, with a fake camera (correct / one-frame lag / timing lag) | seconds |
| `tests/diagnose_exposure.py --hardware` | **real microscope** (no stage motion, illumination flashes) | finds the origin of 'exposure of the previous channel' (see below) | ~5-10 min |
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

## Exposure diagnostic (wrong exposure after channel switch)

    python tests\diagnose_exposure.py --hardware --out-dir D:\exposure_diag --channels "BF LED matrix full,Fluorescence 488 nm Ex,Fluorescence 561 nm Ex" --exposures 10,40,160

Place a static, in-focus sample (not saturated). The stage does not move. The script measures the effective exposure of each frame from the image brightness and tests, step by step: E1 camera register read-back, E2 one channel with alternating exposure (camera/driver only, with 0/0.2/1 s wait), E3 channel switch through the real signal path (first 3 frames after each switch), E3b one discarded dark frame after the switch (old workaround), E5 a real acquisition with the MultiPoint worker (also writes events.csv with all camera calls). Exposures are changed in memory only; the channel configuration file is backed up and restored. Send `report.txt`, `frames.csv`, `events.csv` from `<out-dir>\exposure_diag_<time>`. Without hardware, `--simulation` only checks that the script runs.
