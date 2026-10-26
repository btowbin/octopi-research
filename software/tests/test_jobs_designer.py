"""Tests of the Job designer: documentation vs code, checker, line tracing (no Qt), and the windows (offscreen Qt).

Run:  python tests/test_jobs_designer.py      (needs PyQt5/qtpy for the second half; without Qt only the first half runs)
"""
import os
import sys
import time
import inspect
import tempfile
import textwrap

SOFTWARE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOFTWARE_DIR)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from control.jobs import apidoc  # noqa: E402
from control.jobs.api import Scope, make_points, SimClock  # noqa: E402
from control.jobs.dryrun import DryRunBackend  # noqa: E402
from control.jobs.lint import lint  # noqa: E402
from control.jobs.runner import run_job, dry_run  # noqa: E402

CH = {'BF LED matrix full': dict(exposure_ms=12.0, gain=0, intensity=3), 'Fluorescence 488 nm Ex': dict(exposure_ms=12.0, gain=0, intensity=3),
      'Fluorescence 561 nm Ex': dict(exposure_ms=12.0, gain=0, intensity=3)}
EXAMPLES = os.path.join(SOFTWARE_DIR, 'job_examples')


def _squid_channels():
    """Channels of the real Squid channel configuration (tests/fixtures; has 405, 488, 561, BF, DF) for checking the example jobs."""
    import re
    xml = open(os.path.join(SOFTWARE_DIR, 'tests', 'fixtures', 'squid1_channel_configurations.xml'), encoding='utf-8').read()
    return {n: dict(exposure_ms=12.0, gain=0, intensity=3) for n in re.findall(r'<mode [^>]*?Name="([^"]+)"', xml)}


def _job(text):
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'job.py')
    open(p, 'w').write(textwrap.dedent(text))
    return p


# ---- no Qt -------------------------------------------------------------------------------------
def test_every_public_scope_member_is_documented():
    public = {n for n in dir(Scope) if not n.startswith('_')}
    assert public == set(apidoc.api_names()), (public ^ set(apidoc.api_names()))


def test_documented_signatures_match_the_code():
    for e in apidoc.API:
        if e['kind'] != 'method':
            continue
        sig = inspect.signature(getattr(Scope, e['name']))
        params = [p for p in sig.parameters.values() if p.name != 'self']
        expected = e['signature'].split(e['name'], 1)[1]
        got = '(' + ', '.join(
            ('*' + p.name) if p.kind == p.VAR_POSITIONAL else (p.name if p.default is p.empty else f'{p.name}={p.default!r}'.replace("'", '"') if isinstance(p.default, str) else f'{p.name}={p.default}')
            for p in params) + ')'
        assert expected == got, (e['name'], expected, got)


def test_documented_point_attributes_exist():
    from control.jobs.api import Point, TimePoint
    p, t = Point('A-0', 'A', 0, 0, 1, 2, 3), TimePoint(0, 0.0, 0.0)
    for a in apidoc.POINT_ATTRS:
        assert hasattr(p, a), a
    for a in apidoc.TIMEPOINT_ATTRS:
        assert hasattr(t, a), a


def test_examples_have_no_lint_errors():
    for f in sorted(os.listdir(EXAMPLES)):
        if f.endswith('.py'):
            iss = lint(open(os.path.join(EXAMPLES, f)).read(), _squid_channels(), ['A', 'B', 'default'])
            assert not [i for i in iss if i.severity in ('error', 'warning')], (f, [str(i) for i in iss])


def test_new_job_template_is_clean_and_runs():
    assert lint(apidoc.NEW_JOB_TEMPLATE, CH) == []
    p = _job(apidoc.NEW_JOB_TEMPLATE)
    res = dry_run(p, make_points(['A-0', 'A-1'], [(1, 1, 0), (2, 2, 0)]), CH)
    assert res.status == 'completed' and res.stats['images'] == 20


def test_snippets_are_valid_python_inside_a_job():
    pts = make_points(['A-0', 'A-1', 'B-0'], [(1, 1, 0), (2, 2, 0), (3, 3, 0)])
    for title, desc, code in apidoc.SNIPPETS:
        body = 'def run_job(scope):\n    """x"""\n    t = None\n    pt = scope.points()[0]\n    scope.move_to(pt)\n' + ''.join('    ' + l + '\n' for l in code.splitlines())
        iss = [i for i in lint(body, CH, ['A', 'B']) if i.severity == 'error']
        assert not iss, (title, [str(i) for i in iss])


def test_lint_finds_the_usual_mistakes():
    src = textwrap.dedent('''
        import time
        CHANNELS = ["BF", "gfp"]
        def run_job(scope):
            for t in scope.timepoints(3, interval=10):
                for pt in scope.points("C"):
                    scope.captur("488")
                    scope.capture("488", sav=False)
                    scope.capture()
                    print(pt.indx)
                    time.sleep(2)
                    scope.channels()
    ''')
    msgs = [str(i) for i in lint(src, CH, ['A', 'B'])]
    for needle in ("unknown channel 'gfp'", "no 'captur'", "unexpected keyword argument 'sav'", "missing a required argument",
                   "no 'indx'", 'time.sleep', 'remove the parentheses', "group 'C'", 'never calls scope.move_to'):
        assert any(needle in m for m in msgs), (needle, msgs)


def test_lint_syntax_error_and_missing_run_job():
    assert lint('def run_job(scope:\n  pass')[0].severity == 'error'
    assert 'run_job' in lint('x = 1')[0].message


def test_events_carry_line_numbers_and_errors_have_a_line():
    p = _job('''
        def run_job(scope):
            """x"""
            for pt in scope.points():
                scope.move_to(pt)
                scope.capture("488")
                scope.capture("nonexistent")
    ''')
    pts = make_points(['A-0'], [(1, 1, 0)])
    clock = SimClock()
    res = run_job(p, pts, DryRunBackend(CH, clock=clock), clock=clock, save_images=False, record=True, trace_file=p)
    assert res.status == 'failed' and res.error_line == 7, (res.status, res.error_line)
    lines = {e['kind']: e['line'] for e in res.events if e['kind'] in ('move', 'capture')}
    assert lines['move'] == 5 and lines['capture'] == 6, lines
    p2 = _job('def run_job(scope):\n    x = (\n')
    res = dry_run(p2, pts, CH)
    assert res.status == 'invalid' and res.error_line == 2, (res.status, res.error_line)


def test_parse_positions():
    from control.jobs_designer import parse_positions
    ids, c = parse_positions('id, x_mm, y_mm, z_mm\nA-0, 1, 2, 3\n# c\nB-0; 4; 5\n, 6, 7\n')
    assert ids == ['A-0', 'B-0', ''] and c == [(1, 2, 3), (4, 5), (6, 7)]
    try:
        parse_positions('A-0, 1, 2\nA-1, x, 2')
        raise AssertionError('no error')
    except ValueError as e:
        assert 'line 2' in str(e)


# ---- Qt ---------------------------------------------------------------------------------------
def _qapp():
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait(app, win, timeout=20):
    t = time.time()
    while win.test_run is not None and time.time() - t < timeout:
        app.processEvents()
        time.sleep(0.03)
    app.processEvents()


def test_designer_window_test_plan_and_errors():
    app = _qapp()
    from control.jobs_designer import JobDesignerWindow
    w = JobDesignerWindow(example_dir=EXAMPLES, use_settings=False)
    w.resize(1500, 900); w.show(); app.processEvents()
    w.load_file(os.path.join(EXAMPLES, '02_staggered_autofocus.py'))
    w.run_test(); _wait(app, w)
    assert w.last_result is not None and w.last_result.status == 'completed', w.plan_status.text()
    assert w.plan.events and w.plan.order and 'Test passed' in w.plan_status.text()
    assert w.events_table.rowCount() > 0
    img = w.grab()
    assert not img.isNull()
    # a failing job marks the line
    w.editor.setPlainText('def run_job(scope):\n    """x"""\n    scope.capture("nonexistent")\n')
    w.run_test(); _wait(app, w)
    assert w.last_result.status == 'failed' and w.editor.error_line == 3
    # lint marks
    w.editor.setPlainText('def run_job(scope):\n    scope.captur("488")\n'); w.run_lint()
    assert w.editor.issues.get(2) and 'captur' in w.editor.issues[2][0].message
    # an endless loop is stopped
    import control.jobs_designer as jd
    old, jd.TEST_TIMEOUT_S = jd.TEST_TIMEOUT_S, 1.5
    w.editor.setPlainText('def run_job(scope):\n    """x"""\n    while True:\n        pass\n')
    w.run_test(); _wait(app, w, 15)
    jd.TEST_TIMEOUT_S = old
    assert w.test_run is None and 'does not end' in w.plan_status.text(), w.plan_status.text()
    w.editor.document().setModified(False)
    w.close()


def test_editor_completion_and_help():
    app = _qapp()
    from qtpy.QtCore import Qt
    from qtpy.QtGui import QTextCursor
    from qtpy.QtTest import QTest
    from control.jobs_designer import JobDesignerWindow
    w = JobDesignerWindow(example_dir=EXAMPLES, use_settings=False)
    w.show(); app.processEvents()
    ed = w.editor
    ed.setPlainText('def run_job(scope):\n    for pt in scope.points():\n        ')
    ed.moveCursor(QTextCursor.End)
    QTest.keyClicks(ed, 'scope.'); app.processEvents()
    assert ed.popup.isVisible() and ed.popup.list.count() == len(apidoc.API)
    QTest.keyClicks(ed, 'cap'); app.processEvents()
    assert ed.popup.current_text() == 'capture'
    QTest.keyClick(ed, Qt.Key_Return); app.processEvents()
    assert ed.textCursor().block().text().endswith('scope.capture')
    QTest.keyClicks(ed, '("'); app.processEvents()
    assert ed.popup.isVisible() and ed.popup.list.count() == len(w.setup.channel_infos())
    QTest.keyClicks(ed, '488'); app.processEvents()
    assert ed.popup.list.count() == 1
    QTest.keyClick(ed, Qt.Key_Tab); app.processEvents()
    assert 'Fluorescence 488 nm Ex' in ed.textCursor().block().text()
    ed.popup.hide()
    # attributes of the loop variable
    ed.moveCursor(QTextCursor.End); QTest.keyClick(ed, Qt.Key_Return); QTest.keyClicks(ed, 'pt.'); app.processEvents()
    assert ed.popup.isVisible() and ed.popup.list.count() == len(apidoc.POINT_ATTRS)
    ed.popup.hide()
    # F1 shows the function
    ed.setPlainText('scope.autofocus()'); c = ed.textCursor(); c.setPosition(9); ed.setTextCursor(c)
    w.help_on_word(ed.word_at_cursor()[0])
    assert 'autofocus' in w.help.detail.toPlainText()
    # help search and insert
    w.help.search.setText('timepoints'); app.processEvents()
    assert w.help.tree.currentItem().text(0).startswith('timepoints')
    got = []
    w.help.insert_requested.connect(got.append)
    w.help.btn_ins_ex.click()
    assert got and 'scope.timepoints' in got[0]
    w.editor.document().setModified(False)
    w.close()


def test_jobs_tab_button_opens_designer():
    app = _qapp()
    from qtpy.QtCore import QObject, Signal
    from qtpy.QtWidgets import QLineEdit
    from control.jobs_widget import JobsWidget

    class MPC(QObject):
        signal_job_message = Signal(str)
        signal_job_status = Signal(object)
        acquisitionFinished = Signal()

    class Cfg:
        def __init__(self, n, e):
            self.name, self.exposure_time, self.analog_gain, self.illumination_intensity = n, e, 0.0, 10.0

    class CM:
        configurations = [Cfg('BF LED matrix full', 10.0), Cfg('Fluorescence 488 nm Ex', 10.0)]

    class PW:
        lineEdit_savingDir = QLineEdit()
        location_ids = ['A-0', 'A-1', 'B-0']
        location_list = [[1.0, 1.0, 0.1], [2.0, 1.0, 0.1], [3.0, 2.0, 0.1]]

    cwd = os.getcwd()
    os.chdir(SOFTWARE_DIR)
    try:
        jw = JobsWidget(MPC(), CM(), PW())
        jw.show(); app.processEvents()
        assert jw.btn_designer.text().startswith('Job designer')
        jw.btn_designer.click(); app.processEvents()
        d = jw.designer
        assert d.isVisible()
        assert 'A-0' in d.setup.pos_text.toPlainText() and set(d.setup.channel_infos()) == {'BF LED matrix full', 'Fluorescence 488 nm Ex'}
        # use the job in the JOBs tab
        tmp = _job('def run_job(scope):\n    """x"""\n    for pt in scope.points():\n        scope.move_to(pt)\n        scope.capture("488")\n')
        d.load_file(tmp)
        d.use_in_jobs_tab(); app.processEvents()
        assert jw.lineEdit_job.text() == os.path.abspath(tmp), jw.lineEdit_job.text()
        assert 'OK' in jw.label_status.text()
        # while a job runs the designer must not replace the job
        jw.running = True
        assert 'running' in jw.use_job_from_designer(tmp)
        jw.running = False
        d.editor.document().setModified(False)
        d.close()
        # closing deletes the window (WA_DeleteOnClose); the button must open a new one again, several times
        from qtpy.QtCore import QEvent, QCoreApplication
        for _ in range(3):
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete); app.processEvents()
            jw.btn_designer.click(); app.processEvents()
            assert 'Could not open' not in jw.log.toPlainText(), jw.log.toPlainText()
            d2 = jw.designer
            assert d2 is not None and d2.isVisible(), 'designer did not reopen after closing'
            d2.editor.document().setModified(False)
            d2.close()
    finally:
        os.chdir(cwd)


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    try:
        import qtpy  # noqa: F401
        has_qt = True
    except Exception:
        has_qt = False
    failed = 0
    for n, f in tests:
        qt = n in ('test_designer_window_test_plan_and_errors', 'test_editor_completion_and_help', 'test_jobs_tab_button_opens_designer')
        if qt and not has_qt:
            print('SKIP', n, '(no Qt)')
            continue
        try:
            f()
            print('PASS', n)
        except Exception:
            import traceback
            failed += 1
            print('FAIL', n)
            traceback.print_exc()
    print('failed:', failed)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
