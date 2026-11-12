"""Documentation of the job scripting API: the single source for the Job designer's help panel, tooltips and completion.

No Qt and no hardware here. Every public method/property of `Scope` must have an entry in API (tests/test_jobs_designer.py
checks this, and that the signatures written here match the code), so the help cannot silently fall behind the code.

Structure
  API       list of entries (dict: name, kind, category, signature, summary, details, params, returns, example)
  OBJECTS   the attributes of Point and TimePoint
  TOPICS    longer help pages (title -> HTML), shown in the "Guide" tab
  SNIPPETS  small ready-made pieces of code (title, description, code) the user can insert
  NEW_JOB_TEMPLATE  what "New job" starts with
"""
import html

# ------------------------------------------------------------------------------------------------
# API reference
# ------------------------------------------------------------------------------------------------
CATEGORIES = ['Time', 'Stage', 'Imaging', 'Autofocus', 'Positions and groups', 'Information', 'Messages']

API = [
    # --- Time -----------------------------------------------------------------------------------
    dict(name='timepoints', kind='method', category='Time',
         signature='scope.timepoints(n, interval=None)',
         summary='Loop over n time points; the interval is the time from the START of one time point to the START of the next.',
         details=('Use it as the outer loop of a time lapse. Each pass of the loop gives a <b>TimePoint</b> object with '
                  '<code>.index</code> (0, 1, 2, ...), <code>.start</code> (seconds since the job started) and <code>.late</code> '
                  '(seconds it started later than planned).<br><br>'
                  'If one pass takes <b>longer</b> than the interval, the next time point starts immediately (none is skipped), the delay '
                  'is logged, and the following interval counts from that start. Use <i>Test</i> to see in the plan whether your interval is long enough '
                  '(red dashed lines = late).<br><br>'
                  '<code>n=None</code> runs until you press Abort.'),
         params=[('n', 'number of time points (or None = until aborted)'),
                 ('interval', 'seconds from start to start; None = no waiting between time points')],
         returns='an iterator of TimePoint objects',
         example='for t in scope.timepoints(48, interval=1800):   # 48 time points, every 30 min\n    ...'),
    dict(name='wait', kind='method', category='Time',
         signature='scope.wait(seconds)',
         summary='Wait for a number of seconds (the Abort and Pause buttons keep working).',
         details='Use this instead of <code>time.sleep()</code>: a normal sleep cannot be interrupted by Abort.',
         params=[('seconds', 'how long to wait (>= 0)')],
         returns='nothing',
         example='scope.wait(10)    # let the sample settle for 10 s'),
    dict(name='wait_until', kind='method', category='Time',
         signature='scope.wait_until(elapsed_seconds)',
         summary='Wait until a number of seconds have passed since the START of the job (returns at once if that time has already passed).',
         details=('Good for phases: "do phase 1, then start phase 2 exactly 2 hours after the beginning". '
                  'Time spent <i>paused</i> does not count.'),
         params=[('elapsed_seconds', 'time since the job started, in seconds')],
         returns='nothing',
         example='scope.wait_until(2 * 3600)    # continue 2 h after the start of the job'),

    # --- Stage ----------------------------------------------------------------------------------
    dict(name='move_to', kind='method', category='Stage',
         signature='scope.move_to(point)',
         summary='Move the stage to a position (x, y and z), with the same backlash and safety handling as a normal acquisition.',
         details=('Always call this before <code>capture</code> or <code>autofocus</code> at a position: those work at the '
                  '<b>current</b> stage position. (The Test shows a warning if a loop over positions has no move_to.)<br><br>'
                  'The z that is used is the stored z of the point; after an autofocus it is the focused z.'),
         params=[('point', 'a Point from <code>scope.points()</code>')],
         returns='nothing',
         example='for pt in scope.points():\n    scope.move_to(pt)\n    scope.capture("488")'),

    # --- Imaging --------------------------------------------------------------------------------
    dict(name='capture', kind='method', category='Imaging',
         signature='scope.capture(channel, save=True, compression="lossless", quality=None, ratio=None, save_as=None, filename=None)',
         summary='Take one image in a channel at the current position and return it as a NumPy array. The image is saved automatically.',
         details=('The channel name is the name from the channel configuration; any <b>unique part</b> of it works, '
                  'upper/lower case does not matter (<code>"488"</code> finds <i>Fluorescence 488 nm Ex</i>). '
                  'Unknown or ambiguous names are errors and the message lists the choices.<br><br>'
                  'Exposure, gain and illumination intensity come from the channel configuration (see the <i>Test setup</i> panel). '
                  '<b>File names</b> and <b>save_as</b> (default: the <i>Save as</i> setting of the JOBs tab; a call can override it): <code>"multipage"</code> = ''<code>Time0003_Point0002.tiff</code> in the experiment folder, ONE file for all images taken at that point and time point (channels as pages; written directly from memory when the job moves on); ''<code>"merge"</code> = every image is first written as its own file, and the single files are merged into that multipage file later (while the job waits, or at the end) and then deleted ''(less memory, safer for very large images; lossy compression is applied at the merge); ''<code>"single"</code> = one file per image, <code>3/2_0_0_0_Fluorescence_488_nm_Ex.tiff</code> (time point folder, row number of the position in the table, 0_0_z, channel; as the Flexible Multipoint tab). ''A name that already exists gets <code>_ver_2</code>, <code>_ver_3</code>, ... <b>filename="my_name"</b> replaces the automatic name (no extension), e.g. for special cases where the automatic name is ambiguous. ''Everything is listed in <code>images.csv</code>.<br><br>'
                  '<b>compression</b>: <code>"lossless"</code> (default; zlib-compressed TIFF, identical pixel values) or <code>"lossy"</code> '
                  '(16-bit JPEG2000 inside the TIFF, much smaller files, but part of the camera noise is removed). '
                  'The default is a <b>noise-relative quality</b>: for every channel the quality is chosen automatically, just before writing (in the background), so that the '
                  'codec error is about <b>1x the pixel noise</b> of that channel (noise estimated from the image itself). Dim, noisy channels are thus kept more precisely than '
                  'bright, clean ones, and the compression you get depends on the image (typically ~10x for bright field and a few 10x to several 100x for sparse fluorescence). '
                  'Set a fixed quality with <b>quality</b> (40 to 120 dB; the error is about 65535 / 10^(dB/20) counts, e.g. 60 dB = 65 counts, 80 dB = 6.5 counts; higher = better and bigger files), '
                  'or ask for a size with <b>ratio</b> (target compression: a number for all channels, e.g. <code>ratio=30</code>, or a dictionary for single channels, '
                  'e.g. <code>ratio={"488": 50}</code>; the others then use the noise-relative default): the quality is then chosen automatically per channel just before writing '
                  '(in the background) so that the channel is about that many times smaller than the raw pixels. Give only one of quality and ratio. '
                  'What really happened is written to <code>job_log.txt</code> and <code>lossy_files.csv</code>: achieved ratio, quality and codec error and image noise (counts; for single images only the estimated noise). '
                  'Rule of thumb: the codec error should not be much larger than the image noise. Images with many fine details (e.g. bright field) lose more than sparse fluorescence. '
                  'Lossy needs the Python package <code>imagecodecs</code>; Test tells you if it is missing.'),
         params=[('channel', 'channel name (text), or a unique part of it'),
                 ('save', 'False = only return the image, do not write a file'),
                 ('compression', '"lossless" (default) or "lossy" (JPEG2000)'),
                 ('quality', 'lossy only: PSNR in dB (40-120), default 60'),
                 ('ratio', 'lossy only: target compression, a number or {channel: number}, instead of quality'),
                 ('save_as', '"multipage" (directly), "merge" (single files, merged later) or "single"; default: JOBs tab setting'),
                 ('filename', 'own file name without extension (replaces the automatic one)')],
         returns='the image (NumPy array)',
         example='img = scope.capture("488")\nscope.capture("561", compression="lossy")'),
    dict(name='capture_channels', kind='method', category='Imaging',
         signature='scope.capture_channels(channels, save=True, compression="lossless", quality=None, ratio=None, save_as=None, filename=None)',
         summary='capture() for several channels, one after the other in the given order.',
         details='Same as a loop of <code>capture</code> calls (same compression/quality for all). Returns a dictionary {channel name: image}.',
         params=[('channels', 'list of channel names'), ('save', 'False = do not write files'),
                 ('compression', '"lossless" (default) or "lossy"'), ('quality', 'lossy only: PSNR in dB (default 60)'), ('ratio', 'lossy only: target compression, a number or {channel: number}, instead of quality'),
                 ('save_as', '"multipage" (directly), "merge" (single files, merged later) or "single"; default: JOBs tab setting'),
                 ('filename', 'own file name without extension (single files: name_<channel>)')],
         returns='dict {channel name: image}',
         example='images = scope.capture_channels(["BF", "488", "561"])'),
    dict(name='zstack', kind='method', category='Imaging',
         signature='scope.zstack(channels, planes=None, step_um=None, span_um=None, order="channel_major", compression="lossless", quality=None, ratio=None, save=True, save_as=None, filename=None)',
         summary='Z-stack centred on the current z position, in one or several channels, saved as ONE multipage OME-TIFF.',
         details=('Call <code>move_to</code> (and autofocus) first: the stack is centred on the <b>current z</b>, i.e. the focused z after an autofocus. '
                  'Give <b>two of</b> <code>planes</code>, <code>step_um</code>, <code>span_um</code> (span = (planes - 1) x step; all three are accepted if they agree). '
                  'Examples: <code>planes=11, step_um=1.0</code>, or <code>planes=11, span_um=10</code>, or <code>step_um=1, span_um=10</code>. '
                  'With an even number of planes the centre lies between two planes. Spans above 2000 um are refused.<br><br>'
                  '<b>order</b>: <code>"channel_major"</code> (default) = every channel scans the whole stack, then the next channel; '
                  '<code>"z_major"</code> = at each plane all channels are taken, then z moves on (fewer z moves, better registration between colours, '
                  'but more channel switching).<br><br>'
                  'The stage always scans upwards (increasing z) and returns to the centre afterwards (also after Abort). '
                  'The z offset of each channel from the channel configuration is added to its plane positions, as in the standard acquisition.<br><br>'
                  'File (multipage): <code>Time0003_Point0002_Zstack.tiff</code> (<code>save_as="single"</code>: one file per image, <code>3/2_0_0_&lt;plane&gt;_&lt;channel&gt;.tiff</code>; <code>filename="name"</code>: <code>name_Z0005_&lt;channel&gt;</code>; existing names get <code>_ver_2</code>), axes Z and C, with the real z position, time and exposure of every page in the OME metadata '
                  'and in <code>frames.csv</code>. <b>compression</b> / <b>quality</b> as in <code>capture</code> (default lossless; lossy = JPEG2000 inside the multipage TIFF). '
                  'The whole stack is held in memory until it is written (limit 4 GB).'),
         params=[('channels', 'channel name or list of names'), ('planes', 'number of planes'), ('step_um', 'distance between planes (um)'),
                 ('span_um', 'total span from the first to the last plane (um)'),
                 ('order', '"channel_major" (default) or "z_major"'),
                 ('compression', '"lossless" (default) or "lossy"'), ('quality', 'lossy only: PSNR in dB (default 60)'),
                 ('ratio', 'lossy only: target compression, a number or {channel: number}, instead of quality'),
                 ('save', 'False = do not write a file'),
                 ('save_as', '"multipage" (directly), "merge" (single files, merged later) or "single"; default: JOBs tab setting'),
                 ('filename', 'own file name without extension (replaces the automatic one)')],
         returns='dict {channel name: array of shape (planes, Y, X)}',
         example='scope.move_to(pt)\nscope.autofocus()\nscope.zstack(["488", "561"], planes=11, step_um=1.0, order="z_major")'),
    dict(name='movie', kind='method', category='Imaging',
         signature='scope.movie(channels, frames=None, interval_s=None, duration_s=None, compression="lossless", quality=None, ratio=None, save=True, save_as=None, filename=None)',
         summary='Fast time series at the current position (one or several channels), saved as ONE multipage OME-TIFF with the real time of every image.',
         details=('Each round images all channels in the given order; rounds start <code>interval_s</code> seconds apart (start to start), scheduled against '
                  'a fixed clock so errors do not add up. <code>interval_s=None</code> = back to back, as fast as the camera allows. '
                  'Give <code>frames</code>, or <code>duration_s</code> together with <code>interval_s</code> (frames = duration / interval).<br><br>'
                  'If one round takes longer than the interval the next round starts immediately and a warning reports the achieved interval. '
                  'The achieved interval (mean, sd, min, max) and the start-time error are logged, written into the OME description and, '
                  'per image, as DeltaT in the OME metadata and in <code>frames.csv</code> (time the image was requested, +- exposure and readout). '
                  'For multi-colour movies all channels should have the same exposure (see the exposure warning of Test).<br><br>'
                  'The movie is held in memory and written after the last frame (limit 4 GB; about 300 full 16-bit frames). '
                  'Pause takes effect after the movie, Abort stops it at once (nothing is saved). '
                  '<b>compression</b> / <b>quality</b> as in <code>capture</code>: lossy JPEG2000 is encoded in the background while the job continues.<br><br>'
                  'File (multipage): <code>Time0003_Point0002_Movie.tiff</code> (<code>save_as="single"</code>: one file per image, <code>3/2_0_0_0_&lt;channel&gt;_Movie_F0012.tiff</code>; <code>filename="name"</code>: <code>name_F0012_&lt;channel&gt;</code>); time points of the job are separate files, one movie per call.'),
         params=[('channels', 'channel name or list of names'), ('frames', 'number of rounds'), ('interval_s', 'seconds from the start of one round to the start of the next'),
                 ('duration_s', 'total duration in seconds (needs interval_s; instead of frames)'),
                 ('compression', '"lossless" (default) or "lossy"'), ('quality', 'lossy only: PSNR in dB (default 60)'),
                 ('ratio', 'lossy only: target compression, a number or {channel: number}, instead of quality'),
                 ('save', 'False = do not write a file'),
                 ('save_as', '"multipage" (directly), "merge" (single files, merged later) or "single"; default: JOBs tab setting'),
                 ('filename', 'own file name without extension (replaces the automatic one)')],
         returns='dict {channel name: array of shape (frames, Y, X)}',
         example='scope.move_to(pt)\nscope.movie(["488", "561"], duration_s=60, interval_s=2, compression="lossy")'),

    dict(name='fast_movie', kind='method', category='Imaging',
         signature='scope.fast_movie(channel, fps=25, duration_s=None, frames=None, roi=None, bits=None, exposure_ms=None, gain=None, compression="none", save=True, save_as=None, filename=None)',
         summary='Video-rate movie (e.g. 25 frames/s) in one channel at the current position, frames kept in memory and written afterwards (uncompressed by default).',
         details=('The camera runs freely at <code>fps</code> with the light on only while it records; nothing is processed and nothing is written during the '
                  'recording, so frames are not lost to the disk. Give <code>duration_s</code> or <code>frames</code>.<br><br>'
                  '<b>roi</b>: region in the centre of the sensor, e.g. <code>roi=2000</code> for 2000x2000 pixels (smaller regions are faster). '
                  '<b>bits</b>: <code>8</code> or <code>16</code>; 8 bit reads out about twice as fast and halves the data, but saturates at 255 - check the '
                  'saturation warning and lower <code>exposure_ms</code>, <code>gain</code> or the channel intensity. <b>exposure_ms</b> and <b>gain</b> '
                  'apply to this movie only; all camera settings (bit depth, region, trigger mode, exposure, gain, frame rate) are restored afterwards, '
                  'also after an error, so the next images are as before.<br><br>'
                  'The exposure must be shorter than the frame period (1/fps). The camera frame number and clock of every frame are stored '
                  '(<code>fast_frames.csv</code>, DeltaT in the OME metadata); lost frames, the achieved frame rate, saturation and dark images are reported as warnings. '
                  'The movie must fit in memory (limit 4 GB: 2000x2000 at 8 bit = 1000 frames = 40 s at 25 fps). '
                  'Abort keeps the frames recorded so far; Pause takes effect after the movie.<br><br>'
                  'File: <code>Time0003_Point0002_FastMovie.tiff</code>, uncompressed (<code>compression="lossless"</code> or <code>"lossy"</code> are possible and slow '
                  'to write). <code>save_as</code> and <code>filename</code> as for <code>movie</code> (single files: <code>3/2_0_0_0_&lt;channel&gt;_FastMovie_F0012.tiff</code>). '
                  'Only for FLIR cameras (other cameras: software-triggered frames, slow and irregular).'),
         params=[('channel', 'channel name'), ('fps', 'frames per second'), ('duration_s', 'duration in seconds (instead of frames)'), ('frames', 'number of frames'),
                 ('roi', 'region in the centre of the sensor: side length or (width, height) in pixels; None = current'), ('bits', '8 or 16; None = current'),
                 ('exposure_ms', 'exposure for this movie; None = the channel\'s value'), ('gain', 'analog gain for this movie; None = the channel\'s value'),
                 ('compression', '"none" (default), "lossless" or "lossy"'), ('save', 'False = do not write a file'),
                 ('save_as', '"multipage" (directly), "merge" or "single"; default: JOBs tab setting'), ('filename', 'own file name without extension')],
         returns='dict {channel name: array of shape (frames, Y, X)}',
         example='scope.move_to(pt)\nscope.fast_movie("488", fps=25, duration_s=5, roi=2000, bits=8)'),

    # --- Autofocus ------------------------------------------------------------------------------
    dict(name='autofocus', kind='method', category='Autofocus',
         signature='scope.autofocus(channel=None, focus_measure=None)',
         summary='Contrast autofocus at the current position, with the settings of the Autofocus tab (number of planes, step).',
         details=('The focused z is remembered for the current point, so the next autofocus (and the next visit) starts around it. '
                  'If the new z differs from the starting z by more than 0.5 mm it is <b>not</b> stored and a warning is shown.<br><br>'
                  '<b>channel</b>: which channel is used for focusing (default: the one of the Autofocus tab). '
                  '<b>focus_measure</b>: e.g. <code>"GLVA"</code> or <code>"LAPE"</code>, only for this call; also a custom measure as '
                  '<code>"my_measure.py::my_function"</code> (file relative to the software folder). The Autofocus tab keeps its own setting.<br><br>'
                  'An autofocus takes several seconds: in the plan it is shown as a triangle (estimated duration, see Test setup).'),
         params=[('channel', 'channel for the autofocus (default: Autofocus tab)'),
                 ('focus_measure', 'focus measure for this call only (default: Autofocus tab)')],
         returns='the focused z in mm',
         example='scope.move_to(pt)\nscope.autofocus(channel="561", focus_measure="GLVA")'),

    # --- Positions and groups -------------------------------------------------------------------
    dict(name='points', kind='method', category='Positions and groups',
         signature='scope.points(*groups)',
         summary='The positions of the Flexible Multipoint tab, in table order: all of them, or only those of the given groups.',
         details=('The <b>group</b> of a position is the part of its ID before the first "-" (<code>A-0, A-1, B-0</code> -> groups A and B). '
                  'An empty ID belongs to the group <code>default</code>. Groups let one job treat different samples differently, '
                  'e.g. image group A every 5 min and group B every 30 min.'),
         params=[('*groups', 'none = all points; otherwise group names, e.g. <code>"A"</code> or <code>"A", "B"</code>')],
         returns='list of Point objects',
         example='for pt in scope.points("A", "B"):\n    scope.move_to(pt)'),
    dict(name='groups', kind='property', category='Positions and groups',
         signature='scope.groups',
         summary='Dictionary: group name -> list of its points.',
         details='Handy to loop over groups, or to count them.',
         params=[], returns='dict {group name: [Point, ...]}',
         example='for name, pts in scope.groups.items():\n    scope.log(f"group {name} has {len(pts)} positions")'),
    dict(name='all_points', kind='property', category='Positions and groups',
         signature='scope.all_points',
         summary='All points in table order (same as scope.points()).',
         details='', params=[], returns='list of Point objects',
         example='n = len(scope.all_points)'),

    # --- Information ----------------------------------------------------------------------------
    dict(name='channels', kind='property', category='Information',
         signature='scope.channels',
         summary='The names of the available channels (from the channel configuration).',
         details='Useful to check or loop over channels instead of typing the names.',
         params=[], returns='list of channel names',
         example='scope.log("channels: " + ", ".join(scope.channels))'),
    dict(name='elapsed', kind='property', category='Information',
         signature='scope.elapsed',
         summary='Seconds since the job started (time spent paused is not counted).',
         details='', params=[], returns='float (seconds)',
         example='if scope.elapsed > 6 * 3600:\n    scope.log("6 h are over")'),
    dict(name='experiment_dir', kind='property', category='Information',
         signature='scope.experiment_dir',
         summary='The folder where this experiment is saved (None in a test).',
         details='You may write your own extra files there.',
         params=[], returns='folder path or None',
         example='path = scope.experiment_dir'),
    dict(name='abort_requested', kind='property', category='Information',
         signature='scope.abort_requested',
         summary='True after the user pressed Abort.',
         details=('Every scope call stops the job anyway when Abort was pressed, so you rarely need this. '
                  'The abort is raised as an exception that <code>except Exception:</code> does not catch.'),
         params=[], returns='True / False',
         example='if scope.abort_requested:\n    return'),
    dict(name='check_abort', kind='method', category='Information',
         signature='scope.check_abort()',
         summary='Stop the job now if Abort was pressed.',
         details='Call it inside long pure-Python calculations that do not call scope for a long time.',
         params=[], returns='nothing', example='scope.check_abort()'),

    # --- Messages -------------------------------------------------------------------------------
    dict(name='log', kind='method', category='Messages',
         signature='scope.log(message)',
         summary='Write a message to the JOBs tab and to job_log.txt (with the time since the start).',
         details='In the designer the messages of a test are shown in the Output tab.',
         params=[('message', 'text')], returns='nothing',
         example='scope.log(f"time point {t.index} done")'),
    dict(name='warn', kind='method', category='Messages',
         signature='scope.warn(message)',
         summary='Like log(), but marked as WARNING and listed in the job summary.',
         details='', params=[('message', 'text')], returns='nothing',
         example='scope.warn("autofocus failed at " + pt.id)'),
]

OBJECTS = [
    dict(name='Point', description='One imaging position. You get Points from <code>scope.points()</code>.',
         attrs=[('id', 'text', 'the text typed in the positions table, e.g. "A-3" ("" if none); file names and the csv ID columns use the row number (index) instead'),
                ('group', 'text', 'part of the ID before the first "-" ("default" if the ID is empty)'),
                ('index', 'int', 'position in the table, starting at 0 (all points)'),
                ('number', 'int', 'position within its group, starting at 0'),
                ('x', 'float', 'stage x in mm'),
                ('y', 'float', 'stage y in mm'),
                ('z', 'float', 'stage z in mm (updated after an autofocus)'),
                ('z_initial', 'float', 'z as it was in the table at the start of the job')]),
    dict(name='TimePoint', description='One time point of <code>scope.timepoints()</code>.',
         attrs=[('index', 'int', '0, 1, 2, ...'),
                ('start', 'float', 'seconds since the start of the job when this time point started'),
                ('late', 'float', 'seconds it started later than planned (0 = on time)')]),
]

POINT_ATTRS = [a[0] for a in OBJECTS[0]['attrs']]
TIMEPOINT_ATTRS = [a[0] for a in OBJECTS[1]['attrs']]


def api_by_name():
    return {e['name']: e for e in API}


def api_names():
    return [e['name'] for e in API]


# ------------------------------------------------------------------------------------------------
# HTML for the help panel
# ------------------------------------------------------------------------------------------------
CSS = ('<style>body{font-family:sans-serif;} code,pre{font-family:Consolas,"DejaVu Sans Mono",monospace;background:#f1f1f1;} '
       'pre{padding:6px;} h2{margin-bottom:2px;} h3{margin-bottom:2px;margin-top:10px;} td{padding:2px 8px 2px 0;vertical-align:top;} '
       '.sig{font-size:large;} .muted{color:#666;}</style>')


def entry_html(e):
    parts = [CSS, f'<h2>{html.escape(e["name"])} <span class="muted">({e["kind"]}, {html.escape(e["category"])})</span></h2>',
             f'<p class="sig"><code>{html.escape(e["signature"])}</code></p>', f'<p>{e["summary"]}</p>']
    if e.get('details'):
        parts.append(f'<p>{e["details"]}</p>')
    if e.get('params'):
        parts.append('<h3>Arguments</h3><table>' + ''.join(f'<tr><td><code>{html.escape(n)}</code></td><td>{d}</td></tr>' for n, d in e['params']) + '</table>')
    if e.get('returns'):
        parts.append(f'<h3>Returns</h3><p>{html.escape(e["returns"])}</p>')
    if e.get('example'):
        parts.append('<h3>Example</h3><pre>' + html.escape(e['example']) + '</pre>')
    return ''.join(parts)


def object_html(o):
    var = 'pt' if o['name'] == 'Point' else 't'
    rows = ''.join(f'<tr><td><code>{var}.{html.escape(a)}</code></td><td>{html.escape(t)}</td><td>{html.escape(d)}</td></tr>'
                   for a, t, d in o['attrs'])
    return f'{CSS}<h2>{o["name"]} <span class="muted">(object)</span></h2><p>{o["description"]}</p><table>{rows}</table>'


def tooltip_for(name):
    """Short rich text for a hover tooltip, or None."""
    e = api_by_name().get(name)
    if e is None:
        return None
    return f'<b>{html.escape(e["signature"])}</b><br>{e["summary"]}'


def search(query):
    """Entries (API + objects as pseudo entries) whose name, summary or text contain all the words of the query."""
    words = [w for w in query.lower().split() if w]
    hits = []
    for e in API:
        text = ' '.join([e['name'], e['signature'], e['summary'], e.get('details', ''), e.get('returns', ''),
                         ' '.join(n + ' ' + d for n, d in e.get('params', [])), e['category']]).lower()
        if all(w in text for w in words):
            hits.append(e)
    return hits


# ------------------------------------------------------------------------------------------------
# Guide pages
# ------------------------------------------------------------------------------------------------
TOPICS = {}


def _topic(title, body):
    TOPICS[title] = CSS + body


_topic('Quick start', """
<h2>Quick start</h2>
<p>A <b>job</b> is a short Python program that tells the microscope <i>what</i> to image and <i>when</i>.
It always has this shape:</p>
<pre>def run_job(scope):
    for t in scope.timepoints(10, interval=60):   # 10 time points, one per minute
        for pt in scope.points():                 # every position of the Flexible Multipoint table
            scope.move_to(pt)                     # go there
            scope.capture("488")                  # take an image in the 488 channel</pre>
<p>Everything the microscope does goes through the object <code>scope</code>. You never talk to hardware directly.</p>
<h3>Working in the designer</h3>
<ol>
<li>Type or open a job (<i>File &rarr; New from example</i> is the quickest start).</li>
<li>Press <b>Test (F5)</b>. Nothing moves: the job runs against a simulated microscope with simulated time, so even a 2-day job is checked in a moment.</li>
<li>Look at the <b>Plan</b>: one row per position, one coloured tick per image (colour = channel), triangles = autofocus, dashed lines = time points (red = started late).
Hover for details, click a tick to jump to the line in your code that produced it.</li>
<li>Check the numbers in <b>Output</b> (images, autofocus runs, estimated duration) and the <b>Problems</b> list.</li>
<li>When it looks right: <b>Save</b>, then <b>Use in JOBs tab</b> and start it there.</li>
</ol>
<p>The <i>Test setup</i> panel sets the positions and channels used for the test (they can come from the Flexible Multipoint tab) and the assumed
duration of moves and autofocus. Times in the plan are <b>estimates</b>.</p>
<h3>Getting help while typing</h3>
<ul>
<li>Type <code>scope.</code> to get a list of all functions; <b>Ctrl+Space</b> opens the list anywhere.</li>
<li>Inside <code>scope.capture("</code> the available channel names are offered.</li>
<li>Hover over a function name for a short description; press <b>F1</b> on a name for the full help.</li>
<li>The <i>Functions</i> tab lists everything with examples and an <i>Insert</i> button; <i>Snippets</i> has ready-made pieces such as a staggered autofocus.</li>
<li>Typos, unknown channels and wrong arguments are underlined while you type.</li>
</ul>
""")

_topic('Positions and groups', """
<h2>Positions and groups</h2>
<p>A job does not contain coordinates. It uses the positions you defined in the <b>Flexible Multipoint</b> tab.
Give each position an <b>ID</b> there. The <b>group</b> of a position is the part of its ID before the first "-":</p>
<table><tr><td><code>A-0, A-1, A-2</code></td><td>group <b>A</b></td></tr>
<tr><td><code>B-0, B-1</code></td><td>group <b>B</b></td></tr>
<tr><td><code>(empty)</code></td><td>group <b>default</b></td></tr></table>
<pre>for pt in scope.points("A"):        # only group A
    scope.move_to(pt)
    scope.capture("488")
for pt in scope.points():           # everything, in table order
    ...</pre>
<p>Each point knows its <code>id</code>, <code>group</code>, <code>index</code> (0-based in the table), <code>number</code> (0-based in its group), and <code>x y z</code>.
Use <code>pt.number</code> to treat every n-th position of a group differently, e.g. <code>if pt.number % 2 == 0:</code>.</p>
<p>In the designer, the positions for the test come from the <i>Test setup</i> panel. Use <i>Load from Flexible Multipoint</i> to copy the real ones,
or <i>Sample positions</i> to try your job on a made-up layout.</p>
""")

_topic('Time points and timing', """
<h2>Time points and timing</h2>
<pre>for t in scope.timepoints(48, interval=1800):
    ...</pre>
<p>The <b>interval</b> is measured from the <b>start</b> of one time point to the <b>start</b> of the next. If a time point takes longer than the interval,
the next one starts immediately - none is skipped - and the delay is logged. The plan marks such time points in red, and the
<i>Time per time point</i> chart shows when your work does not fit into the interval.</p>
<p>Different schedules for different positions or channels are just <code>if</code> statements on the time point index:</p>
<pre>if t.index % 4 == 0:                 # every 4th time point
    scope.capture("BF")
if (t.index + pt.index) % 5 == 0:    # staggered: different positions at different time points
    scope.autofocus()</pre>
<p><code>%</code> is the remainder after division: <code>7 % 5</code> is 2. <code>t.index % 4 == 0</code> is true at 0, 4, 8, ...</p>
<p>For phases use <code>scope.wait_until(seconds)</code>: wait until a number of seconds after the job start. For a pause use <code>scope.wait(seconds)</code>.
Never use <code>time.sleep</code>: it cannot be aborted.</p>
""")

_topic('Channels and exposure', """
<h2>Channels and exposure</h2>
<p>Channels are the ones of the channel configuration of your microscope (see the <i>Test setup</i> panel for the list). Use the full name or any unique part of it, ignoring case:
<code>"488"</code>, <code>"bf"</code>, <code>"Fluorescence 561 nm Ex"</code>. If a name is not unique, the error message lists the matches.</p>
<p>Exposure time, gain and intensity are taken from the channel configuration; a job cannot change them (yet). Edit them in the main program, or - to see what would happen to the timing - in the
<i>Test setup</i> panel (this affects only the test).</p>
<p><b>Known issue:</b> with channels of <i>different</i> exposure times the camera may use the previous channel's exposure after a channel switch. The test warns if the used channels have different exposures.</p>
<p>The channel names can be collected at the top of the file as a setting, so they are easy to change:</p>
<pre>CHANNELS = ["BF", "488", "561"]
...
for ch in CHANNELS:
    scope.capture(ch)</pre>
""")

_topic('Autofocus', """
<h2>Autofocus</h2>
<p><code>scope.autofocus()</code> focuses at the current position using the settings of the Autofocus tab (number of planes, step size, focus measure, channel). You can override
the channel and the focus measure for one call:</p>
<pre>scope.autofocus(channel="561", focus_measure="GLVA")</pre>
<p>The focused z is stored in the point, so the next autofocus searches around it. An autofocus takes several seconds. To save time,
focus only some positions per time point (<i>staggered</i>):</p>
<pre>if t.index == 0 or (t.index + pt.index) % 5 == 0:
    scope.autofocus()</pre>
<p>Here every position is focused at the first time point and afterwards every 5th time point, each position at a different time point. See the snippet <i>Staggered autofocus</i>.</p>
<p>A custom focus measure from your own file: <code>focus_measure="my_measure.py::my_function"</code> (file relative to the software folder).</p>
""")

_topic('Pause, abort, errors', """
<h2>Pause, abort, errors</h2>
<p><b>Abort</b> (JOBs tab) stops the job at the next scope call, switches the illumination off, and finishes the saved files cleanly. The abort is raised as an exception inside your job that
<code>except Exception:</code> does not catch, so your job cannot accidentally ignore it.</p>
<p><b>Pause</b> holds the job between two operations; you can move the stage and edit positions, then resume. Job time stands still while paused.</p>
<p>Errors in your job (a typo, an unknown channel) are reported with the line number, the light is switched off and the experiment is closed cleanly. Errors the test finds
prevent the start in the JOBs tab. That is why you should always press <b>Test</b> first.</p>
<p>The test cannot know what image <i>content</i> will look like (it uses dummy images), so a job that decides based on the image (<code>img.mean()</code>) may behave differently on the microscope.</p>
""")

_topic('Files that are written', """
<h2>Files in the experiment folder</h2>
<table>
<tr><td><code>Time0000_Point0000.tiff, ...</code></td><td>multipage files (<i>Save as</i> multipage or merge): one per point and time point, <code>..._Zstack.tiff</code>, <code>..._Movie.tiff</code></td></tr>
<tr><td><code>0/, 1/ ...</code></td><td>single files (<i>Save as</i> single files): one folder per time point, <code>&lt;ID&gt;_0_0_&lt;z&gt;_&lt;channel&gt;.tiff</code>; an existing name gets <code>_ver_2</code></td></tr>
<tr><td><code>images.csv</code></td><td>one row per image: file, time, time point, position, channel, actual x/y/z, exposure, gain, intensity</td></tr>
<tr><td><code>autofocus.csv</code></td><td>one row per autofocus: z before/after, duration</td></tr>
<tr><td><code>job.py</code></td><td>copy of the job that was run</td></tr>
<tr><td><code>job_log.txt</code></td><td>what happened and when (late time points, warnings, your <code>scope.log</code> messages)</td></tr>
<tr><td><code>job_meta.json</code></td><td>status, duration, counts, warnings, software version</td></tr>
<tr><td><code>positions_initial.csv, positions_final.csv</code></td><td>positions before and after (z after autofocus)</td></tr>
</table>
""")

_topic('Python in 2 minutes', """
<h2>Python in 2 minutes</h2>
<p>You only need a few ideas.</p>
<h3>Indentation matters</h3>
<p>The lines that belong to a <code>for</code> or <code>if</code> are indented by 4 spaces (the Tab key does that). Lines with the same indentation belong together.</p>
<h3>Repeat: for</h3>
<pre>for pt in scope.points():     # once for each position; pt is the current one
    scope.move_to(pt)
for i in range(5):            # i = 0, 1, 2, 3, 4
    scope.log(i)
for ch in ["BF", "488"]:      # once for each item of a list
    scope.capture(ch)</pre>
<h3>Decide: if</h3>
<pre>if t.index == 0:                       # == compares; = assigns
    scope.autofocus()
elif t.index % 2 == 0 and pt.group == "A":
    scope.capture("BF")
else:
    pass                               # do nothing</pre>
<p><code>%</code> remainder, <code>==</code> equal, <code>!=</code> different, <code>and</code> / <code>or</code> / <code>not</code>.</p>
<h3>Variables and settings</h3>
<pre>N_TIMEPOINTS = 24             # a setting at the top of the file
INTERVAL_S = 30 * 60          # 30 minutes in seconds
img = scope.capture("488")    # img is a NumPy array: img.mean(), img.max(), ...</pre>
<h3>Text</h3>
<pre>scope.log(f"time point {t.index}, position {pt.id}")     # f-strings put values into text</pre>
<h3>Comments</h3>
<p>Everything after <code>#</code> is ignored. <b>Ctrl+/</b> comments out the selected lines.</p>
<h3>The docstring</h3>
<p>The text in triple quotes right after <code>def run_job(scope):</code> is shown when the job is opened in the JOBs tab. Say there what the job does and what
the user has to provide (positions, channels).</p>
""")


# ------------------------------------------------------------------------------------------------
# Snippets
# ------------------------------------------------------------------------------------------------
SNIPPETS = [
    ('Z-stack at every position',
     '11 planes, 1 um apart, centred on the (autofocused) z; two channels, one multipage TIFF per position.',
     'scope.move_to(pt)\nscope.autofocus()\nscope.zstack(["488", "561"], planes=11, step_um=1.0)\n'),
    ('Movie at a position',
     'Two minutes, one round every 2 s, two colours, saved as lossy JPEG2000 to save disk space.',
     'scope.move_to(pt)\nscope.movie(["488", "561"], duration_s=120, interval_s=2, compression="lossy")\n'),
    ('Video-rate movie (25 fps)',
     '5 s at 25 frames/s, 2000x2000 pixels, 8 bit, uncompressed; the camera settings are restored afterwards.',
     'scope.move_to(pt)\nscope.fast_movie("488", fps=25, duration_s=5, roi=2000, bits=8)\n'),
    ('Time lapse over all positions',
     'Every position, one channel, at regular intervals.',
     'for t in scope.timepoints(10, interval=60):\n    for pt in scope.points():\n        scope.move_to(pt)\n        scope.capture("488")\n'),
    ('Several channels per position',
     'All channels of a list at every position.',
     'CHANNELS = ["BF", "488", "561"]\n\nfor pt in scope.points():\n    scope.move_to(pt)\n    for ch in CHANNELS:\n        scope.capture(ch)\n'),
    ('Autofocus at every position',
     'Move, focus, then image.',
     'scope.move_to(pt)\nscope.autofocus()\nscope.capture("488")\n'),
    ('Staggered autofocus',
     'All positions are focused at the first time point; afterwards only every 5th time point, each position at a different one.',
     'if t.index == 0 or (t.index + pt.index) % 5 == 0:\n    scope.autofocus()\n'),
    ('Every N-th time point',
     'A channel (e.g. bright field) only at some time points.',
     'if t.index % 4 == 0:        # time points 0, 4, 8, ...\n    scope.capture("BF")\n'),
    ('Only some groups',
     'Loop over the positions of groups A and B only (group = part of the ID before the first "-").',
     'for pt in scope.points("A", "B"):\n    scope.move_to(pt)\n    scope.capture("488")\n'),
    ('Different groups, different channels',
     'Group A gets GFP, group B gets RFP.',
     'for pt in scope.points():\n    scope.move_to(pt)\n    if pt.group == "A":\n        scope.capture("488")\n    elif pt.group == "B":\n        scope.capture("561")\n'),
    ('Two phases',
     'Fast imaging first, then slow imaging starting 2 hours after the beginning.',
     'for t in scope.timepoints(24, interval=300):        # phase 1: every 5 min for 2 h\n    for pt in scope.points():\n        scope.move_to(pt)\n        scope.capture("488")\n\nscope.wait_until(2 * 3600)\n\nfor t in scope.timepoints(12, interval=3600):       # phase 2: every hour\n    for pt in scope.points():\n        scope.move_to(pt)\n        scope.capture("488")\n'),
    ('Every n-th position of a group',
     'pt.number counts within the group, starting at 0.',
     'for pt in scope.points("A"):\n    if pt.number % 2 == 0:       # every second position of group A\n        scope.move_to(pt)\n        scope.capture("488")\n'),
    ('Decide from the image',
     'Take a second image only if the first one is bright (works on the microscope; in a test the images are dummy zeros).',
     'img = scope.capture("488")\nif img.mean() > 200:\n    scope.capture("561")\n'),
    ('Log a message',
     'Written to the JOBs tab and job_log.txt.',
     'scope.log(f"time point {t.index}: position {pt.id} done")\n'),
    ('Settings block',
     'Put the numbers you change often at the top of the file.',
     '# ---- settings: edit these ----\nCHANNELS = ["BF", "488"]\nN_TIMEPOINTS = 12\nINTERVAL_S = 600      # seconds from start to start\n'),
]

NEW_JOB_TEMPLATE = '''"""Short title of the job (one line)."""

# ---- settings: edit these ------------------------------------------------------------------
CHANNELS = ["488"]          # channel names (any unique part of the name works)
N_TIMEPOINTS = 10
INTERVAL_S = 60             # seconds from the START of one time point to the START of the next


def run_job(scope):
    """WHAT IT DOES: images all positions of the Flexible Multipoint table in CHANNELS at every time point.

    YOU PROVIDE:
      - Positions in the Flexible Multipoint tab (stage x, y and a start z close to focus).
      - The settings at the top of the file (channels, number of time points, interval).
    """
    for t in scope.timepoints(N_TIMEPOINTS, interval=INTERVAL_S):
        for pt in scope.points():
            scope.move_to(pt)
            for ch in CHANNELS:
                scope.capture(ch)
'''
