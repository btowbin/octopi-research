"""Camera tab of the main window: camera ROI (the only crop) and camera mode (bit depth).

The ROI is a hardware ROI, always centred on the sensor, with width and height rounded to
the nearest multiple of 8. The software crops of the live stream, the multipoint acquisition
and the autofocus are always set to the same size, i.e. there is no additional software
cropping: autofocus crop = acquisition (saving) crop = display crop = ROI.
"""
import json
import os
from qtpy.QtWidgets import QFrame, QGridLayout, QLabel, QSpinBox, QComboBox, QCheckBox
from control._def import STARTUP_DEFAULT_ROI_WIDTH, STARTUP_DEFAULT_ROI_HEIGHT

OPTIONS_FILE = os.path.join('cache', 'camera_options.json')

# The camera rounds the offsets (and the size) to its own increments (up to 16 px on FLIR), so a ROI counts as centred
# if it is within this many pixels of the exact centre.
CENTRE_TOLERANCE_PX = 16


class CameraROIWidget(QFrame):

    def __init__(self, camera, stream_handler=None, multipoint_controller=None,
                 autofocus_controller=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.camera = camera
        self.stream_handler = stream_handler
        self.multipoint_controller = multipoint_controller
        self.autofocus_controller = autofocus_controller
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

        sensor_w, sensor_h = self._sensor_size()

        # what the camera REALLY has (read back from the camera after every change and before every acquisition)
        self.label_actual = QLabel('')
        self.label_actual.setStyleSheet('font-size: 8pt; color: gray;')
        self.label_actual.setWordWrap(True)
        self.label_linearity = QLabel('')
        self.label_linearity.setStyleSheet('font-size: 8pt; color: gray;')
        self.label_linearity.setWordWrap(True)

        self.spin_width = QSpinBox()
        self.spin_width.setRange(16, sensor_w)
        self.spin_width.setSingleStep(8)
        self.spin_width.setSuffix(' px')
        self.spin_width.setKeyboardTracking(False)  # only apply on Enter / focus-out
        self.spin_height = QSpinBox()
        self.spin_height.setRange(16, sensor_h)
        self.spin_height.setSingleStep(8)
        self.spin_height.setSuffix(' px')
        self.spin_height.setKeyboardTracking(False)

        # start from the camera's actual ROI and bring all software crops in line with it
        width, height = self._current_roi_size()
        self.spin_width.setValue(width)
        self.spin_height.setValue(height)
        self._apply_software_crops(width, height)

        # startup default ROI (_def.STARTUP_DEFAULT_ROI_WIDTH / _HEIGHT; None keeps the camera's ROI):
        # set on the camera exactly as if typed into the spin boxes (the signals are not connected
        # yet, so update_roi is called explicitly)
        if STARTUP_DEFAULT_ROI_WIDTH is not None or STARTUP_DEFAULT_ROI_HEIGHT is not None:
            if STARTUP_DEFAULT_ROI_WIDTH is not None:
                self.spin_width.setValue(self.valid_size(STARTUP_DEFAULT_ROI_WIDTH, sensor_w))
            if STARTUP_DEFAULT_ROI_HEIGHT is not None:
                self.spin_height.setValue(self.valid_size(STARTUP_DEFAULT_ROI_HEIGHT, sensor_h))
            self.update_roi()
        else:
            self.check_roi(fix=True)    # an off-centre ROI left by an earlier session is centred now

        self.show_linearity()

        self.dropdown_pixel_format = QComboBox()
        formats = ['MONO8', 'MONO12', 'MONO14', 'MONO16']
        if getattr(self.camera, 'is_color', None):
            formats += ['BAYER_RG8', 'BAYER_RG12']
        self.dropdown_pixel_format.addItems(formats)
        current_format = getattr(self.camera, 'pixel_format', None)
        if current_format in formats:
            self.dropdown_pixel_format.setCurrentText(current_format)

        info = QLabel('ROI is centred on the sensor; width and height are rounded to multiples of 8.\n'
                      'It is the only crop: live view, autofocus and saved images use the full ROI.')
        info.setStyleSheet('font-size: 8pt; color: gray;')

        grid = QGridLayout()
        grid.addWidget(QLabel('ROI width'), 0, 0)
        grid.addWidget(self.spin_width, 0, 1)
        grid.addWidget(QLabel('ROI height'), 0, 2)
        grid.addWidget(self.spin_height, 0, 3)
        grid.addWidget(QLabel('Camera mode'), 1, 0)
        grid.addWidget(self.dropdown_pixel_format, 1, 1)
        grid.addWidget(info, 2, 0, 1, 4)
        grid.addWidget(self.label_actual, 3, 0, 1, 4)
        grid.addWidget(self.label_linearity, 4, 0, 1, 4)
        self.check_empty_frame = None
        if hasattr(self.camera, 'exposure_lag_fix'):      # FLIR: an exposure takes effect one frame late
            self.check_empty_frame = QCheckBox('Empty frame in between')
            self.check_empty_frame.setToolTip(
                'This camera uses a new exposure time one frame late (each image gets the exposure of the previous channel).\n'
                'Ticked: after every exposure change an empty dummy frame (exposed with the OLD exposure) is taken and thrown away,\n'
                'so the next image is correct. Costs about one old exposure + readout per channel change.\n'
                'Not ticked: an experiment with channels of different exposure times is not started.\n'
                'Not needed if all channels have the same exposure time.')
            try:
                with open(OPTIONS_FILE, encoding='utf-8') as f:
                    self.check_empty_frame.setChecked(bool(json.load(f).get('exposure_lag_fix', False)))
            except Exception:
                pass
            self.camera.exposure_lag_fix = self.check_empty_frame.isChecked()
            self.check_empty_frame.toggled.connect(self.set_empty_frame)
            grid.addWidget(self.check_empty_frame, 5, 0, 1, 4)
        grid.setRowStretch(grid.rowCount(), 1)
        self.setLayout(grid)

        self.spin_width.valueChanged.connect(self.update_roi)
        self.spin_height.valueChanged.connect(self.update_roi)
        self.dropdown_pixel_format.currentTextChanged.connect(self.set_pixel_format)

        # before every acquisition / job: the hardware ROI must still be centred and equal to the software crops
        if self.multipoint_controller is not None:
            self.multipoint_controller.before_start_hook = self.check_roi

    def set_empty_frame(self, on):
        self.camera.exposure_lag_fix = bool(on)
        try:
            os.makedirs(os.path.dirname(OPTIONS_FILE), exist_ok=True)
            with open(OPTIONS_FILE, 'w', encoding='utf-8') as f:
                json.dump({'exposure_lag_fix': bool(on)}, f)
        except Exception as e:
            print('could not save', OPTIONS_FILE, e)

    # ---- helpers ---------------------------------------------------------------------
    def _sensor_size(self):
        """Full sensor size in px (3000 x 3000 if the camera does not report it)."""
        try:
            return int(self.camera.WidthMax), int(self.camera.HeightMax)
        except (AttributeError, TypeError):
            return 3000, 3000

    def _current_roi_size(self):
        sensor_w, sensor_h = self._sensor_size()
        try:
            width = self.camera.Width if self.camera.Width else sensor_w
            height = self.camera.Height if self.camera.Height else sensor_h
        except AttributeError:
            width, height = sensor_w, sensor_h
        return int(width), int(height)

    @staticmethod
    def centred_offset(sensor_size, size):
        """Offset (multiple of 8) that centres a ROI of this size on the sensor."""
        return max(0, ((int(sensor_size) - int(size)) // 2) // 8 * 8)

    def _read_hw_roi(self):
        """(offset_x, offset_y, width, height) as the camera really has them, or None if this camera class cannot tell."""
        getter = getattr(self.camera, 'get_ROI', None)
        if getter is None:
            return None
        try:
            return tuple(int(v) for v in getter())
        except Exception as e:
            print(f'Camera ROI could not be read back from the camera: {e}')
            return None

    @staticmethod
    def valid_size(value, sensor_size):
        """Nearest multiple of 8, at least 16 and at most the sensor size (rounded down to a multiple of 8)."""
        largest = max(16, (int(sensor_size) // 8) * 8)
        return int(min(max(int(value / 8 + 0.5) * 8, 16), largest))

    def _apply_software_crops(self, width, height):
        """Keep every software crop identical to the hardware ROI (= no software cropping)."""
        if self.multipoint_controller is not None:
            self.multipoint_controller.crop_width = width
            self.multipoint_controller.crop_height = height
        if self.stream_handler is not None:
            self.stream_handler.set_crop(width, height)
        if self.autofocus_controller is not None:
            self.autofocus_controller.set_crop(width, height)

    # ---- actions ---------------------------------------------------------------------
    def update_roi(self):
        """Set the centred hardware ROI and the equal software crops."""
        sensor_w, sensor_h = self._sensor_size()
        width = self.valid_size(self.spin_width.value(), sensor_w)
        height = self.valid_size(self.spin_height.value(), sensor_h)

        # show the rounded values without re-triggering this method
        for spin, value in ((self.spin_width, width), (self.spin_height, height)):
            if spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

        error = None
        try:
            self._write_roi(width, height, sensor_w, sensor_h)
        except Exception as e:      # the three writes are not atomic: a failure can leave the offsets at 0 -> checked and repaired below
            error = e
            print(f'*** Camera ROI could not be set to {width} x {height}: {e}')
        if self._read_hw_roi() is None:
            # this camera class cannot report its ROI: nothing to verify, trust what was requested (as before)
            if error is not None:
                raise error
            self._apply_software_crops(width, height)
            return
        self.check_roi(fix=True)
        if error is not None:
            self._show_status(f'ROI change to {width} x {height} FAILED ({error}); the camera keeps the ROI shown here.', warning=True)

    def _write_roi(self, width, height, sensor_w, sensor_h):
        offset_x = self.centred_offset(sensor_w, width)
        offset_y = self.centred_offset(sensor_h, height)
        # offsets to 0 first so that the new size always fits (the maximum width/height of e.g. a FLIR
        # camera is sensor size minus the CURRENT offset), then the size, then the centred offsets
        self.camera.set_ROI(offset_x=0, offset_y=0)
        self.camera.set_ROI(width=width, height=height)
        self.camera.set_ROI(offset_x=offset_x, offset_y=offset_y)
        print(f'Camera ROI set: offset_x={offset_x}, offset_y={offset_y}, width={width}, height={height}')

    def check_roi(self, fix=True):
        """Read the ROI back from the camera and compare it with the centred ROI of the same size.
        fix=True: if it is off centre, centre it again (offsets only, the size is not touched). In every case the spin boxes and
        all software crops are set to the size the camera REALLY has, so crop = hardware ROI and the image centre is the sensor centre.
        Called after every ROI change, at start-up and before every acquisition / job. -> True if centred (or if the camera cannot tell)."""
        actual = self._read_hw_roi()
        if actual is None:
            return True
        sensor_w, sensor_h = self._sensor_size()
        ox, oy, w, h = actual

        def centre_error(ox, oy, w, h):
            return ox - (sensor_w - w) / 2.0, oy - (sensor_h - h) / 2.0
        dx, dy = centre_error(ox, oy, w, h)
        repaired = False
        if fix and (abs(dx) > CENTRE_TOLERANCE_PX or abs(dy) > CENTRE_TOLERANCE_PX):
            print(f'*** Camera ROI is NOT centred (offset {ox}, {oy}, size {w} x {h}, centre error {dx:+.0f}, {dy:+.0f} px): centring it again')
            try:
                self.camera.set_ROI(offset_x=self.centred_offset(sensor_w, w), offset_y=self.centred_offset(sensor_h, h))
            except Exception as e:
                print(f'*** could not centre the camera ROI: {e}')
            again = self._read_hw_roi()
            if again is not None:
                ox, oy, w, h = again
                dx, dy = centre_error(ox, oy, w, h)
            repaired = True
        centred = abs(dx) <= CENTRE_TOLERANCE_PX and abs(dy) <= CENTRE_TOLERANCE_PX
        # spin boxes and software crops follow what the camera really has
        for spin, value in ((self.spin_width, w), (self.spin_height, h)):
            if spin.value() != value:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self._apply_software_crops(w, h)
        text = f'Camera ROI (read from the camera): offset {ox}, {oy}, size {w} x {h}, centre error {dx:+.0f}, {dy:+.0f} px'
        if repaired and centred:
            text += ' - was off centre, centred again'
        if not centred:
            text += ' - NOT CENTRED'
        self._show_status(text, warning=not centred)
        self.show_linearity()
        return centred

    def show_linearity(self):
        """Read gamma / lookup table / black level back from the camera and show them (read-only; nothing is written).
        Gamma on or a lookup table on means the pixel values are NOT linear in the light: shown in red and printed with ***."""
        label = getattr(self, 'label_linearity', None)
        getter = getattr(self.camera, 'get_linearity_state', None)
        if getter is None:
            text, bad = 'Gamma: this camera class cannot report it (the software does not set it; the camera default applies).', False
        else:
            try:
                st = getter()
            except Exception as e:
                st, text, bad = None, f'Gamma: could not be read from the camera ({e})', False
            if st is not None:
                def onoff(v):
                    return 'unknown' if v is None else ('ON' if v else 'off')
                gamma_on, lut_on = st.get('gamma_enabled'), st.get('lut_enabled')
                bad = bool(gamma_on) or bool(lut_on)
                text = 'Camera gamma (read from the camera): ' + onoff(gamma_on)
                if gamma_on and st.get('gamma') is not None:
                    text += f" (value {st['gamma']:.2f})"
                text += ', lookup table: ' + onoff(lut_on)
                if st.get('black_level') is not None:
                    text += f", black level: {st['black_level']:g}"
                if gamma_on is None:
                    text += ' - gamma state unknown'
                text += ' - NOT LINEAR' if bad else (' - linear' if gamma_on is False and not lut_on else '')
        if bad:
            print('*** ' + text)
        if label is not None:
            label.setText(text)
            label.setStyleSheet('font-size: 8pt; color: ' + ('red' if bad else 'gray') + ';')

    def check_roi_if_idle(self):
        """check_roi, but only when no experiment / job is running: used when live view is switched on (live view is also restarted
        inside a running experiment, e.g. after an autofocus; the ROI is checked once at the START of an experiment, never during it)."""
        try:
            if self.multipoint_controller is not None and self.multipoint_controller.acquisition_running():
                return True
        except Exception:
            pass
        return self.check_roi()

    def _show_status(self, text, warning=False):
        label = getattr(self, 'label_actual', None)
        if label is not None:
            label.setText(text)
            label.setStyleSheet('font-size: 8pt; color: ' + ('red' if warning else 'gray') + ';')

    def set_pixel_format(self, pixel_format):
        """Camera mode / bit depth."""
        self.camera.set_pixel_format(pixel_format)
