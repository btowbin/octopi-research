"""Hourly 6-week time lapse: 488 + 561 + BF, staggered autofocus and staggered BF movies every 6 hours."""

# ---- settings: edit these ------------------------------------------------------------------
CHANNELS = ["405", "488", "561", "BF LED matrix full"]   # imaged in this order at every position and time point
AF_CHANNEL = "561"                                # channel used for the autofocus
AF_FOCUS_MEASURE = "focus_measure_normalized_variance.py::normalized_variance"   # maximal normalized variance (file in the software folder)
MOVIE_CHANNEL = "BF LED matrix full"
N_TIMEPOINTS = 6 * 7 * 24                         # 6 weeks, hourly = 1008 time points
INTERVAL_S = 3600                                 # 1 h from the START of one time point to the START of the next
STAGGER_EVERY = 6                                 # autofocus and movie of a position every 6th time point (= every 6 h)
MOVIE_FRAMES = 10                                 # 10 images, one per second = 10 s movie
MOVIE_FRAME_INTERVAL_S = 1.0


def run_job(scope):
    """WHAT IT DOES: all positions of the Flexible Multipoint table are imaged every hour for 6 weeks (IDs / groups are ignored).
    At every time point each position is imaged in 405, 488, 561 and BF, in this order.
    Autofocus (channel 561, focus measure = maximal normalized variance) runs
      - at the FIRST time point at ALL positions, and
      - afterwards staggered: a position with table index k is refocused at the time points t >= 1 where (t + k) is a
        multiple of STAGGER_EVERY, i.e. every 6 h, and only 1/6 of the positions per time point.
    The focused z is stored for each position, so the next autofocus searches around it. The autofocus is done BEFORE the images.
    Movies: at the same staggered time points (t >= 1, every 6 h per position, same time points as the autofocus) a 10 s BF movie is
    recorded after the normal images: 10 BF images, one per second. Movie frames are saved as <position>_BF..._2, _3, ... in the
    time point folder (the first BF image of the time point is the normal one).

    YOU PROVIDE:
      - Positions in the Flexible Multipoint tab (stage x, y and a start z close to focus; IDs / groups are not needed).
      - The channel names above must match the channel configuration (check with the designer: Test).
      - The custom focus measure file focus_measure_normalized_variance.py in the software folder.
      - Number of planes and step of the autofocus: Autofocus tab.
      - Exposure, intensity and gain come from the channel configuration. Use the same exposure for all channels
        (known issue, the check warns otherwise). Check that one pass of all positions fits into 1 h (Plan: working time per time point).
    """
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points():
            scope.move_to(pt)
            staggered = (t.index + pt.index) % STAGGER_EVERY == 0
            if t.index == 0 or staggered:
                scope.autofocus(channel=AF_CHANNEL, focus_measure=AF_FOCUS_MEASURE)
            for ch in CHANNELS:
                scope.capture(ch)
            if t.index >= 1 and staggered:
                start = scope.elapsed
                for i in range(MOVIE_FRAMES):
                    scope.wait_until(start + i * MOVIE_FRAME_INTERVAL_S)    # one frame per second (an image takes < 1 s)
                    scope.capture(MOVIE_CHANNEL)