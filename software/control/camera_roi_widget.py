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
            grid.addWidget(self.check_empty_frame, 3, 0, 1, 4)
        grid.setRowStretch(grid.rowCount(), 1)
        self.setLayout(grid)

        self.spin_width.valueChanged.connect(self.update_roi)
        self.spin_height.valueChanged.connect(self.update_roi)
        self.dropdown_pixel_format.currentTextChanged.connect(self.set_pixel_format)

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

        offset_x = max(0, ((sensor_w - width) // 2) // 8 * 8)
        offset_y = max(0, ((sensor_h - height) // 2) // 8 * 8)
        self.camera.set_ROI(offset_x=offset_x, offset_y=offset_y, width=width, height=height)
        print(f'Camera ROI set: offset_x={offset_x}, offset_y={offset_y}, width={width}, height={height}')
        self._apply_software_crops(width, height)

    def set_pixel_format(self, pixel_format):
        """Camera mode / bit depth."""
        self.camera.set_pixel_format(pixel_format)
