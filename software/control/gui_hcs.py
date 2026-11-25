# set QT_API environment variable
import os 
import sys
import time
import json
os.environ["QT_API"] = "pyqt5"
import qtpy

# qt libraries
from qtpy.QtCore import *
from qtpy.QtWidgets import *
from qtpy.QtGui import *

from control._def import *

# app specific libraries
import control.widgets as widgets
from control.camera_roi_widget import CameraROIWidget
from control.autofocus_settings_widget import AutofocusSettingsWidget
import serial

if CAMERA_TYPE == "Toupcam":
    try:
        import control.camera_toupcam as camera
    except:
        print("Problem importing Toupcam, defaulting to default camera")
        import control.camera as camera
elif CAMERA_TYPE == "FLIR":
    try:
        import control.camera_flir as camera
    except:
        print("Problem importing FLIR camera, defaulting to default camera")
        import control.camera as camera
else:
    import control.camera as camera

if FOCUS_CAMERA_TYPE == "Toupcam":
    try:
        import control.camera_toupcam as camera_fc
    except:
        print("Problem importing Toupcam for focus, defaulting to default camera")
        import control.camera as camera_fc
elif FOCUS_CAMERA_TYPE == "FLIR":
    try:
        import control.camera_flir as camera_fc
    except:
        print("Problem importing FLIR camera for focus, defaulting to default camera")
        import control.camera as camera_fc
else:
    import control.camera as camera_fc



import control.core as core
import control.microcontroller as microcontroller

import control.serial_peripherals as serial_peripherals

if SUPPORT_LASER_AUTOFOCUS:
    import control.core_displacement_measurement as core_displacement_measurement

import pyqtgraph.dockarea as dock
import time

SINGLE_WINDOW = True # set to False if use separate windows for display and control

class OctopiGUI(QMainWindow):

    # variables
    fps_software_trigger = 100

    def __init__(self, is_simulation = False, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # load objects
        if is_simulation:
            if ENABLE_SPINNING_DISK_CONFOCAL:
                self.xlight = serial_peripherals.XLight_Simulation()
            if ENABLE_NL5:
                import control.NL5 as NL5
                self.nl5 = NL5.NL5_Simulation()
            if ENABLE_CELLX:
                self.cellx = serial_peripherals.CellX_Simulation()
            if SUPPORT_LASER_AUTOFOCUS:
                self.camera_focus = camera_fc.Camera_Simulation()
            self.camera = camera.Camera_Simulation(rotate_image_angle=ROTATE_IMAGE_ANGLE,flip_image=FLIP_IMAGE)
            self.camera.set_pixel_format(DEFAULT_PIXEL_FORMAT)

            if USE_ZABER_EMISSION_FILTER_WHEEL:
                self.emission_filter_wheel = serial_peripherals.FilterController_Simulation(115200, 8, serial.PARITY_NONE, serial.STOPBITS_ONE)
            if USE_OPTOSPIN_EMISSION_FILTER_WHEEL:
                self.emission_filter_wheel = serial_peripherals.Optospin_Simulation(SN=None)

            self.microcontroller = microcontroller.Microcontroller_Simulation()
        else:
            if ENABLE_SPINNING_DISK_CONFOCAL:
                self.xlight = serial_peripherals.XLight(XLIGHT_SERIAL_NUMBER,XLIGHT_SLEEP_TIME_FOR_WHEEL)
            if ENABLE_NL5:
                print('initializing NL5 ...')
                import control.NL5 as NL5
                self.nl5 = NL5.NL5()
                print('NL5 initialized')
            if ENABLE_CELLX:
                print('initializing CellX ...')
                self.cellx = serial_peripherals.CellX(CELLX_SN)
                for channel in [1,2,3,4]:
                    self.cellx.set_modulation(channel,CELLX_MODULATION)
                    self.cellx.turn_on(channel)
                print('CellX initialized')
            if USE_LDI_SERIAL_CONTROL:
                print('initializing LDI ...')
                self.ldi = serial_peripherals.LDI()
                self.ldi.run()
                print('LDI initialized')
                self.ldi.set_intensity_mode(LDI_INTENSITY_MODE)
                self.ldi.set_shutter_mode(LDI_SHUTTER_MODE)
            if SUPPORT_LASER_AUTOFOCUS:
                sn_camera_focus = camera_fc.get_sn_by_model(FOCUS_CAMERA_MODEL)
                self.camera_focus = camera_fc.Camera(sn=sn_camera_focus)
                self.camera_focus.open()
                self.camera_focus.set_pixel_format('MONO8')
            sn_camera_main = camera.get_sn_by_model(MAIN_CAMERA_MODEL)
            self.camera = camera.Camera(sn=sn_camera_main,rotate_image_angle=ROTATE_IMAGE_ANGLE,flip_image=FLIP_IMAGE)
            self.camera.open()
            self.camera.set_pixel_format(DEFAULT_PIXEL_FORMAT) # bug: comment out for confocal?

            if USE_ZABER_EMISSION_FILTER_WHEEL:
                self.emission_filter_wheel = serial_peripherals.FilterController(FILTER_CONTROLLER_SERIAL_NUMBER, 115200, 8, serial.PARITY_NONE, serial.STOPBITS_ONE)
            if USE_OPTOSPIN_EMISSION_FILTER_WHEEL:
                self.emission_filter_wheel = serial_peripherals.Optospin(SN=FILTER_CONTROLLER_SERIAL_NUMBER)

            self.microcontroller = microcontroller.Microcontroller(version=CONTROLLER_VERSION,sn=CONTROLLER_SN)

        if USE_ZABER_EMISSION_FILTER_WHEEL:
            self.emission_filter_wheel.start_homing()
        if USE_OPTOSPIN_EMISSION_FILTER_WHEEL:
            self.emission_filter_wheel.set_speed(OPTOSPIN_EMISSION_FILTER_WHEEL_SPEED_HZ)

        # reset the MCU
        self.microcontroller.reset()
        time.sleep(0.5)

        # reinitialize motor drivers and DAC (in particular for V2.1 driver board where PG is not functional)
        self.microcontroller.initialize_drivers()
        time.sleep(0.5)

        # configure the actuators
        self.microcontroller.configure_actuators()

        print('load channel_configurations.xml')
        self.configurationManager = core.ConfigurationManager(filename='./channel_configurations.xml')
        if STARTUP_DEFAULT_EXPOSURE_MS is not None: # startup default: the same exposure for all illuminations (see _def.py)
            self.configurationManager.set_all_exposures_without_writing(STARTUP_DEFAULT_EXPOSURE_MS)
        self.objectiveStore = core.ObjectiveStore() # todo: add widget to select/save objective save
        self.streamHandler = core.StreamHandler(display_resolution_scaling=DEFAULT_DISPLAY_CROP/100)
        self.liveController = core.LiveController(self.camera,self.microcontroller,self.configurationManager,parent=self)
        self.navigationController = core.NavigationController(self.microcontroller, self.objectiveStore, parent=self)
        self.slidePositionController = core.SlidePositionController(self.navigationController,self.liveController,is_for_wellplate=True)
        self.autofocusController = core.AutoFocusController(self.camera,self.navigationController,self.liveController)
        self.scanCoordinates = core.ScanCoordinates()
        self.multipointController = core.MultiPointController(self.camera,self.navigationController,self.liveController,self.autofocusController,self.configurationManager,scanCoordinates=self.scanCoordinates,parent=self)
        if ENABLE_TRACKING:
            self.trackingController = core.TrackingController(self.camera,self.microcontroller,self.navigationController,self.configurationManager,self.liveController,self.autofocusController,self.imageDisplayWindow)
        self.imageSaver = core.ImageSaver()
        self.imageDisplay = core.ImageDisplay()
        self.navigationViewer = core.NavigationViewer(self.objectiveStore, sample=str(WELLPLATE_FORMAT)+' well plate')
        # retract the objective
        self.navigationController.home_z()
        # wait for the operation to finish
        t0 = time.time()
        while self.microcontroller.is_busy():
            time.sleep(0.005)
            if time.time() - t0 > 10:
                print('z homing timeout, the program will exit')
                sys.exit(1)
        print('objective retracted')

        # set encoder arguments
        # set axis pid control enable
        # only ENABLE_PID_X and HAS_ENCODER_X are both enable, can be enable to PID
        if HAS_ENCODER_X == True:
            self.navigationController.set_axis_PID_arguments(0, PID_P_X, PID_I_X, PID_D_X)
            self.navigationController.configure_encoder(0, (SCREW_PITCH_X_MM * 1000) / ENCODER_RESOLUTION_UM_X, ENCODER_FLIP_DIR_X)
            self.navigationController.set_pid_control_enable(0, ENABLE_PID_X)
        if HAS_ENCODER_Y == True:
            self.navigationController.set_axis_PID_arguments(1, PID_P_Y, PID_I_Y, PID_D_Y)
            self.navigationController.configure_encoder(1, (SCREW_PITCH_Y_MM * 1000) / ENCODER_RESOLUTION_UM_Y, ENCODER_FLIP_DIR_Y)
            self.navigationController.set_pid_control_enable(1, ENABLE_PID_Y)
        if HAS_ENCODER_Z == True:
            self.navigationController.set_axis_PID_arguments(2, PID_P_Z, PID_I_Z, PID_D_Z)
            self.navigationController.configure_encoder(2, (SCREW_PITCH_Z_MM * 1000) / ENCODER_RESOLUTION_UM_Z, ENCODER_FLIP_DIR_Z)
            self.navigationController.set_pid_control_enable(2, ENABLE_PID_Z)
        time.sleep(0.5)

        self.navigationController.set_z_limit_pos_mm(SOFTWARE_POS_LIMIT.Z_POSITIVE)
        self.navigationController.set_z_limit_neg_mm(SOFTWARE_POS_LIMIT.Z_NEGATIVE)

        # home XY, set zero and set software limit
        print('home xy')
        timestamp_start = time.time()
        # x needs to be at > + 20 mm when homing y
        self.navigationController.move_x(20) # to-do: add blocking code
        while self.microcontroller.is_busy():
            time.sleep(0.005)
        # home y
        self.navigationController.home_y()
        t0 = time.time()
        while self.microcontroller.is_busy():
            time.sleep(0.005)
            if time.time() - t0 > 10:
                print('y homing timeout, the program will exit')
                sys.exit(1)
        self.navigationController.zero_y()
        # home x
        self.navigationController.home_x()
        t0 = time.time()
        while self.microcontroller.is_busy():
            time.sleep(0.005)
            if time.time() - t0 > 10:
                print('x homing timeout, the program will exit')
                sys.exit(1)
        self.navigationController.zero_x()
        self.slidePositionController.homing_done = True

        if USE_ZABER_EMISSION_FILTER_WHEEL:
            self.emission_filter_wheel.wait_for_homing_complete()

        self.navigationController.set_x_limit_pos_mm(SOFTWARE_POS_LIMIT.X_POSITIVE)
        self.navigationController.set_x_limit_neg_mm(SOFTWARE_POS_LIMIT.X_NEGATIVE)
        self.navigationController.set_y_limit_pos_mm(SOFTWARE_POS_LIMIT.Y_POSITIVE)
        self.navigationController.set_y_limit_neg_mm(SOFTWARE_POS_LIMIT.Y_NEGATIVE)
        self.navigationController.set_z_limit_pos_mm(SOFTWARE_POS_LIMIT.Z_POSITIVE)

        # move to scanning position
        self.navigationController.move_x(20)
        while self.microcontroller.is_busy():
            time.sleep(0.005)
        self.navigationController.move_y(20)
        while self.microcontroller.is_busy():
            time.sleep(0.005)

        # set piezo arguments
        if ENABLE_OBJECTIVE_PIEZO is True:
            if OBJECTIVE_PIEZO_CONTROL_VOLTAGE_RANGE == 5:
                OUTPUT_GAINS.CHANNEL7_GAIN = True
            else:
                OUTPUT_GAINS.CHANNEL7_GAIN = False

        # set output's gains
        div = 1 if OUTPUT_GAINS.REFDIV is True else 0
        gains  = OUTPUT_GAINS.CHANNEL0_GAIN << 0 
        gains += OUTPUT_GAINS.CHANNEL1_GAIN << 1 
        gains += OUTPUT_GAINS.CHANNEL2_GAIN << 2 
        gains += OUTPUT_GAINS.CHANNEL3_GAIN << 3 
        gains += OUTPUT_GAINS.CHANNEL4_GAIN << 4 
        gains += OUTPUT_GAINS.CHANNEL5_GAIN << 5 
        gains += OUTPUT_GAINS.CHANNEL6_GAIN << 6 
        gains += OUTPUT_GAINS.CHANNEL7_GAIN << 7 
        self.microcontroller.configure_dac80508_refdiv_and_gain(div, gains)

        # set illumination intensity factor
        global ILLUMINATION_INTENSITY_FACTOR
        self.microcontroller.set_dac80508_scaling_factor_for_illumination(ILLUMINATION_INTENSITY_FACTOR)

        # open the camera
        # camera start streaming
        # self.camera.set_reverse_x(CAMERA_REVERSE_X) # these are not implemented for the cameras in use
        # self.camera.set_reverse_y(CAMERA_REVERSE_Y) # these are not implemented for the cameras in use
        self.camera.set_software_triggered_acquisition() #self.camera.set_continuous_acquisition()
        self.camera.set_callback(self.streamHandler.on_new_frame)
        self.camera.enable_callback()

        # load widgets
        if ENABLE_SPINNING_DISK_CONFOCAL:
            self.spinningDiskConfocalWidget = widgets.SpinningDiskConfocalWidget(self.xlight, self.configurationManager)
        if ENABLE_NL5:
            import control.NL5Widget as NL5Widget
            self.nl5Wdiget = NL5Widget.NL5Widget(self.nl5)

        # Camera tab: centred hardware ROI (the only crop) and camera mode; replaces widgets.CameraSettingsWidget
        self.cameraROIWidget = CameraROIWidget(self.camera,self.streamHandler,self.multipointController,self.autofocusController)
        self.liveController.before_live_hook = self.cameraROIWidget.check_roi_if_idle # centred ROI whenever live view is switched on (not inside a running experiment)
        self.liveControlWidget = widgets.LiveControlWidget(self.streamHandler,self.liveController,self.configurationManager,show_display_options=True,show_autolevel=True,autolevel=True)
        self.navigationWidget = widgets.NavigationWidget(self.navigationController,self.slidePositionController,widget_configuration=f'{WELLPLATE_FORMAT} well plate')
        self.dacControlWidget = widgets.DACControWidget(self.microcontroller)
        self.autofocusWidget = AutofocusSettingsWidget(self.autofocusController,self.configurationManager)
        if USE_ZABER_EMISSION_FILTER_WHEEL or USE_OPTOSPIN_EMISSION_FILTER_WHEEL:
            self.filterControllerWidget = widgets.FilterControllerWidget(self.emission_filter_wheel, self.liveController)
        self.recordingControlWidget = widgets.RecordingWidget(self.streamHandler,self.imageSaver)
        if ENABLE_TRACKING:
            self.trackingControlWidget = widgets.TrackingControllerWidget(self.trackingController,self.configurationManager,show_configurations=TRACKING_SHOW_MICROSCOPE_CONFIGURATIONS)
        self.multiPointWidget = widgets.MultiPointWidget(self.multipointController,self.configurationManager)
        self.multiPointWidget2 = widgets.MultiPointWidget2(self.navigationController,self.navigationViewer,self.multipointController,self.configurationManager,scanCoordinates=None) # =self.scanCoordinates
        from control.jobs_widget import JobsWidget
        self.jobsWidget = JobsWidget(self.multipointController,self.configurationManager,self.multiPointWidget2) # JOBs: scripted acquisition on the positions of the flexible multipoint tab
        from control.single_measurement_widget import SingleMeasurementWidget
        self.singleMeasurementWidget = SingleMeasurementWidget(self.multipointController,self.configurationManager,self.multiPointWidget2) # quick single capture / fast timelapse at the current position
        from control.illumination_table_widget import IlluminationTableWidget
        self.illuminationTableWidget = IlluminationTableWidget(self.configurationManager,self.multipointController,self.camera) # table of the channels: edit, duplicate, delete, add from the permanent list
        self.piezoWidget = widgets.PiezoWidget(self.navigationController)
        self.wellplateFormatWidget = widgets.WellplateFormatWidget()
        self.objectivesWidget = widgets.ObjectivesWidget(self.objectiveStore)
        if WELLPLATE_FORMAT != 1536:
            self.wellSelectionWidget = widgets.WellSelectionWidget(WELLPLATE_FORMAT)
        else:
            self.wellSelectionWidget = widgets.Well1536SelectionWidget()
        self.scanCoordinates.add_well_selector(self.wellSelectionWidget)

        # image display tabs
        self.imageDisplayTabs = QTabWidget()

        if USE_NAPARI_FOR_LIVE_VIEW:
            self.napariLiveWidget = widgets.NapariLiveWidget(self.configurationManager, self.liveControlWidget)
            self.imageDisplayTabs.addTab(self.napariLiveWidget, "Live View")
        else:
            if ENABLE_TRACKING:
                self.imageDisplayWindow = core.ImageDisplayWindow(draw_crosshairs=True)
                self.imageDisplayWindow.show_ROI_selector()
            else:
                self.imageDisplayWindow = core.ImageDisplayWindow(draw_crosshairs=True,show_LUT=True,autoLevels=True)
            self.imageDisplayTabs.addTab(self.imageDisplayWindow.widget, "Live View")

        if USE_NAPARI_FOR_MULTIPOINT:
            self.napariMultiChannelWidget = widgets.NapariMultiChannelWidget(self.configurationManager)
            # self.napariMultiChannelWidget.set_pixel_size_um(3.76*2/60)  
            # ^ for 60x, IMX571, 2x2 binning, to change to using objective and camera config
            self.imageDisplayTabs.addTab(self.napariMultiChannelWidget, "Multichannel Acquisition")
        else:
            self.imageArrayDisplayWindow = core.ImageArrayDisplayWindow()
            self.imageDisplayTabs.addTab(self.imageArrayDisplayWindow.widget, "Multichannel Acquisition")

        if SHOW_TILED_PREVIEW:
            if USE_NAPARI_FOR_TILED_DISPLAY:
                self.napariTiledDisplayWidget = widgets.NapariTiledDisplayWidget(self.configurationManager)
                self.imageDisplayTabs.addTab(self.napariTiledDisplayWidget, "Tiled Preview")
            else:
                self.imageDisplayWindow_scan_preview = core.ImageDisplayWindow(draw_crosshairs=True) 
                self.imageDisplayTabs.addTab(self.imageDisplayWindow_scan_preview.widget, "Tiled Preview")

        # acquisition tabs
        self.recordTabWidget = QTabWidget()
        # the wellplate tab is hidden unless enabled, but never leave the GUI without a multipoint tab
        self.show_wellplate_tab = ENABLE_WELLPLATE_MULTIPOINT or not ENABLE_FLEXIBLE_MULTIPOINT
        if self.show_wellplate_tab:
            self.recordTabWidget.addTab(self.multiPointWidget, "Multipoint (Wellplate)")
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.recordTabWidget.addTab(self.multiPointWidget2, "Flexible Multipoint")
            self.recordTabWidget.addTab(self.jobsWidget, "JOBs")
            self.recordTabWidget.addTab(self.singleMeasurementWidget, "Single measurement")
        if ENABLE_SPINNING_DISK_CONFOCAL:
            self.recordTabWidget.addTab(self.spinningDiskConfocalWidget,"Spinning Disk Confocal")
        if ENABLE_TRACKING:
            self.recordTabWidget.addTab(self.trackingControlWidget, "Tracking")
        self.recordTabWidget.addTab(self.recordingControlWidget, "Simple Recording")

        self.microscopeControlTabWidget = QTabWidget()
        self.microscopeControlTabWidget.addTab(self.navigationWidget,"Stages")
        if ENABLE_OBJECTIVE_PIEZO:
            self.microscopeControlTabWidget.addTab(self.piezoWidget,"Piezo")
        self.microscopeControlTabWidget.addTab(self.autofocusWidget,"Contrast AF")
        if USE_ZABER_EMISSION_FILTER_WHEEL or USE_OPTOSPIN_EMISSION_FILTER_WHEEL:
            self.microscopeControlTabWidget.addTab(self.filterControllerWidget,"Emission Filter")
        self.microscopeControlTabWidget.addTab(self.cameraROIWidget,"Camera")
        self.microscopeControlTabWidget.addTab(self.illuminationTableWidget,"Illuminations")

        # Creating the frame widget
        frame = QFrame()
        frame.setFrameStyle(QFrame.Panel | QFrame.Raised)
        # Creating the top row layout and adding widgets
        top_row_layout = QHBoxLayout()
        top_row_layout.addWidget(self.objectivesWidget)
        top_row_layout.addWidget(self.wellplateFormatWidget)
        # the control panel can be shown in one column (default) or two columns (image display gets the rest of the window)
        self.checkbox_two_columns = QCheckBox('2 columns')
        self.checkbox_two_columns.setToolTip('Show the menus on the right in two columns (the image display gets the remaining width). The choice is remembered.')
        frame_layout = QVBoxLayout()
        frame_layout.addLayout(top_row_layout)
        frame_layout.addWidget(self.checkbox_two_columns)   # second row: does not make the panel wider
        frame.setLayout(frame_layout)  # Set the layout on the frame
        # column 1: objective/plate, live control, stages/AF/camera tabs; column 2: acquisition tabs and the navigation viewer
        self._panel_col1_widgets = [frame, self.liveControlWidget, self.microscopeControlTabWidget]
        if SHOW_DAC_CONTROL:
            self._panel_col1_widgets.append(self.dacControlWidget)
        self._panel_col2_widgets = [self.recordTabWidget, self.navigationViewer]

        # transfer the layout to the central widget
        self.centralWidget = QWidget()
        self._panel_col1 = QVBoxLayout()
        self._panel_col2 = QVBoxLayout()
        self._panel_root = QHBoxLayout()
        self._panel_root.addLayout(self._panel_col1)
        self._panel_root.addLayout(self._panel_col2)   # stays empty in one-column mode
        self._panel_col1.addStretch()
        self._panel_col2.addStretch()
        self.centralWidget.setLayout(self._panel_root)
        self._panel_two_columns = False
        self._arrange_control_panel(self._load_two_column_preference())
        self.checkbox_two_columns.setChecked(self._panel_two_columns)
        self.checkbox_two_columns.toggled.connect(self.set_two_column_panel)

        if SINGLE_WINDOW:
            dock_display = dock.Dock('Image Display', autoOrientation = False)
            dock_display.showTitleBar()
            dock_display.addWidget(self.imageDisplayTabs)
            dock_display.setStretch(x=100,y=100)
            self.dock_wellSelection = dock.Dock('Well Selector', autoOrientation = False)
            self.dock_wellSelection.showTitleBar()
            self.dock_wellSelection.addWidget(self.wellSelectionWidget)
            #self.dock_wellSelection.addWidget(self.wellplateFormatWidget) # todo: add widget to select wellplate format
            self.dock_wellSelection.setFixedHeight(self.dock_wellSelection.minimumSizeHint().height())
            dock_controlPanel = dock.Dock('Controls', autoOrientation = False)
            # dock_controlPanel.showTitleBar()
            from control.resizable_panel import ResizableControlPanel
            self.controlPanel = ResizableControlPanel(self.centralWidget)   # scroll area: the divider to the image display can be dragged
            dock_controlPanel.addWidget(self.controlPanel)
            dock_controlPanel.setStretch(x=1,y=None)
            self._control_panel_width = self.controlPanel.sizeHint().width()
            main_dockArea = dock.DockArea()
            main_dockArea.addDock(dock_display)
            main_dockArea.addDock(self.dock_wellSelection,'bottom')
            if not self.show_wellplate_tab:
                self.dock_wellSelection.setVisible(False) # well selector belongs to the wellplate tab
            main_dockArea.addDock(dock_controlPanel,'right')
            self.setCentralWidget(main_dockArea)
            self.main_dockArea = main_dockArea
            QTimer.singleShot(0, self.init_control_panel_width)
            desktopWidget = QDesktopWidget()
            height_min = 0.9*desktopWidget.height()
            width_min = 0.96*desktopWidget.width()
            #self.setMinimumSize(int(width_min),int(height_min))
        else:
            self.setCentralWidget(self.centralWidget)
            self.tabbedImageDisplayWindow = QMainWindow()
            self.tabbedImageDisplayWindow.setCentralWidget(self.imageDisplayTabs)
            self.tabbedImageDisplayWindow.setWindowFlags(self.windowFlags() | Qt.CustomizeWindowHint)
            self.tabbedImageDisplayWindow.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)
            desktopWidget = QDesktopWidget()
            width = 0.96*desktopWidget.height()
            height = width
            self.tabbedImageDisplayWindow.setFixedSize(int(width), int(height))
            self.tabbedImageDisplayWindow.show()

        '''
        try:
            self.cswWindow = widgets.WrapperWindow(self.cameraSettingWidget)
        except AttributeError:
            pass

        try:
            self.cswfcWindow = widgets.WrapperWindow(self.cameraSettingWidget_focus_camera)
        except AttributeError:
            pass
        '''

        # make connections
        self.streamHandler.signal_new_frame_received.connect(self.liveController.on_new_frame)          
        self.streamHandler.packet_image_to_write.connect(self.imageSaver.enqueue)
        # self.streamHandler.packet_image_for_tracking.connect(self.trackingController.on_new_frame)
        self.navigationController.xPos.connect(lambda x:self.navigationWidget.label_Xpos.setText("{:.2f}".format(x)))
        self.navigationController.yPos.connect(lambda x:self.navigationWidget.label_Ypos.setText("{:.2f}".format(x)))
        self.navigationController.zPos.connect(lambda x:self.navigationWidget.label_Zpos.setText("{:.2f}".format(x)))
        if ENABLE_TRACKING:
            self.navigationController.signal_joystick_button_pressed.connect(self.trackingControlWidget.slot_joystick_button_pressed)
        else:
            self.navigationController.signal_joystick_button_pressed.connect(self.autofocusController.autofocus_single)

        self.multiPointWidget.signal_acquisition_started.connect(self.navigationWidget.toggle_navigation_controls)
        self.multiPointWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.recordTabWidget.currentChanged.connect(self.onTabChanged)
            self.multiPointWidget2.signal_acquisition_started.connect(self.navigationWidget.toggle_navigation_controls)
            self.multiPointWidget2.signal_acquisition_started.connect(self.toggleAcquisitionStart)
            self.jobsWidget.signal_acquisition_started.connect(self.navigationWidget.toggle_navigation_controls)
            self.jobsWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)
            self.jobsWidget.signal_job_paused.connect(self.toggleJobPause)
            self.singleMeasurementWidget.signal_acquisition_started.connect(self.navigationWidget.toggle_navigation_controls)
            self.singleMeasurementWidget.signal_acquisition_started.connect(self.toggleAcquisitionStart)
        # stability: tell the user when an acquisition ended because of an error (it used to hang silently)
        self.multipointController.signal_acquisition_error.connect(self.show_acquisition_error)
        # stability: lock mouse / keyboard input while an acquisition runs (lifted while a JOB is paused)
        from control.input_lock import InputLock
        self.inputLock = InputLock(self._input_locked_windows, self._input_allowed_widgets,
                                   status=lambda text: self.statusBar().showMessage(text, 4000), parent=self)
        self.label_acquisition_running = QLabel('  ACQUISITION RUNNING - controls locked (Stop / Pause available)  ')
        self.label_acquisition_running.setStyleSheet('color: white; background-color: #c0392b; font-weight: bold;')
        self.label_acquisition_running.setVisible(False)
        self.statusBar().addPermanentWidget(self.label_acquisition_running)
        self.multipointController.acquisitionStarted.connect(self._on_acquisition_started_lock)
        self.multipointController.acquisitionFinished.connect(self._on_acquisition_finished_lock)
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.jobsWidget.signal_job_paused.connect(self._on_job_paused_lock)

        # exposure/gain edits in the live control widget are applied directly to the camera
        # (previously relayed through the hidden spin boxes of the removed camera settings widget)
        self.liveControlWidget.signal_newExposureTime.connect(self.camera.set_exposure_time)
        self.liveControlWidget.signal_newAnalogGain.connect(self.camera.set_analog_gain)
        self.liveControlWidget.update_camera_settings()
        self.objectivesWidget.signal_objective_changed.connect(self.navigationViewer.on_objective_changed)

        # load vs scan position switching
        self.slidePositionController.signal_slide_loading_position_reached.connect(self.navigationWidget.slot_slide_loading_position_reached)
        self.slidePositionController.signal_slide_loading_position_reached.connect(self.multiPointWidget.disable_the_start_aquisition_button)
        self.slidePositionController.signal_slide_scanning_position_reached.connect(self.navigationWidget.slot_slide_scanning_position_reached)
        self.slidePositionController.signal_slide_scanning_position_reached.connect(self.multiPointWidget.enable_the_start_aquisition_button)
        self.slidePositionController.signal_clear_slide.connect(self.navigationViewer.clear_slide)

        # display the FOV in the viewer
        self.navigationController.xyPos.connect(self.navigationViewer.update_current_location)
        self.multipointController.signal_register_current_fov.connect(self.navigationViewer.register_fov)
        self.multipointController.signal_current_configuration.connect(self.liveControlWidget.set_microscope_mode)
        self.autofocusController.signal_set_configuration.connect(self.liveControlWidget.set_microscope_mode) # single autofocus: AF channel
        self.multipointController.signal_z_piezo_um.connect(self.piezoWidget.update_displacement_um_display)

        if USE_NAPARI_FOR_LIVE_VIEW:
            self.autofocusController.image_to_display.connect(lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=True))
            self.streamHandler.image_to_display.connect(lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=False))
            self.multipointController.image_to_display.connect(lambda image: self.napariLiveWidget.updateLiveLayer(image, from_autofocus=False))
            self.napariLiveWidget.signal_coordinates_clicked.connect(self.navigationController.move_from_click)
        else:
            self.streamHandler.image_to_display.connect(self.imageDisplay.enqueue)
            self.imageDisplay.image_to_display.connect(self.imageDisplayWindow.display_image) # may connect streamHandler directly to imageDisplayWindow
            self.autofocusController.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.multipointController.image_to_display.connect(self.imageDisplayWindow.display_image)
            self.liveControlWidget.signal_autoLevelSetting.connect(self.imageDisplayWindow.set_autolevel) # todo: replicate for napari (autolevel)
            self.imageDisplayWindow.image_click_coordinates.connect(self.navigationController.move_from_click)
        
        if USE_NAPARI_FOR_MULTIPOINT:    
            self.multiPointWidget.signal_acquisition_channels.connect(self.napariMultiChannelWidget.initChannels)
            self.multiPointWidget.signal_acquisition_shape.connect(self.napariMultiChannelWidget.initLayersShape)
            if ENABLE_FLEXIBLE_MULTIPOINT:
                self.multiPointWidget2.signal_acquisition_channels.connect(self.napariMultiChannelWidget.initChannels)
                self.multiPointWidget2.signal_acquisition_shape.connect(self.napariMultiChannelWidget.initLayersShape)
            self.multipointController.napari_layers_init.connect(self.napariMultiChannelWidget.initLayers)
            self.multipointController.napari_layers_update.connect(self.napariMultiChannelWidget.updateLayers)
            if USE_NAPARI_FOR_LIVE_VIEW:
                self.napariMultiChannelWidget.signal_layer_contrast_limits.connect(self.napariLiveWidget.saveContrastLimits)
                self.napariLiveWidget.signal_layer_contrast_limits.connect(self.napariMultiChannelWidget.saveContrastLimits)
        else:
            self.multipointController.image_to_display_multi.connect(self.imageArrayDisplayWindow.display_image)

        if SHOW_TILED_PREVIEW:
            if USE_NAPARI_FOR_TILED_DISPLAY:
                self.multiPointWidget.signal_acquisition_channels.connect(self.napariTiledDisplayWidget.initChannels)
                self.multiPointWidget.signal_acquisition_shape.connect(self.napariTiledDisplayWidget.initLayersShape)
                if ENABLE_FLEXIBLE_MULTIPOINT:
                    self.multiPointWidget2.signal_acquisition_channels.connect(self.napariTiledDisplayWidget.initChannels)
                    self.multiPointWidget2.signal_acquisition_shape.connect(self.napariTiledDisplayWidget.initLayersShape)
                self.multipointController.napari_layers_init.connect(self.napariTiledDisplayWidget.initLayers)
                self.multipointController.napari_layers_update.connect(self.napariTiledDisplayWidget.updateLayers)
                self.napariTiledDisplayWidget.signal_coordinates_clicked.connect(self.navigationController.scan_preview_move_from_click)
                if USE_NAPARI_FOR_LIVE_VIEW:
                    self.napariTiledDisplayWidget.signal_layer_contrast_limits.connect(self.napariLiveWidget.saveContrastLimits)
                    self.napariLiveWidget.signal_layer_contrast_limits.connect(self.napariTiledDisplayWidget.saveContrastLimits)
                if USE_NAPARI_FOR_MULTIPOINT:
                    self.napariTiledDisplayWidget.signal_layer_contrast_limits.connect(self.napariMultiChannelWidget.saveContrastLimits)
                    self.napariMultiChannelWidget.signal_layer_contrast_limits.connect(self.napariTiledDisplayWidget.saveContrastLimits)
            else:
                self.multipointController.image_to_display_tiled_preview.connect(self.imageDisplayWindow_scan_preview.display_image)
                self.imageDisplayWindow_scan_preview.image_click_coordinates.connect(self.navigationController.scan_preview_move_from_click)

        # (double) click to move to a well
        self.wellplateFormatWidget.signalWellplateSettings.connect(self.wellSelectionWidget.updateWellplateSettings)
        self.wellplateFormatWidget.signalWellplateSettings.connect(self.navigationViewer.update_wellplate_settings)
        self.wellplateFormatWidget.signalWellplateSettings.connect(self.scanCoordinates.update_wellplate_settings)
        self.wellplateFormatWidget.signalWellplateSettings.connect(lambda format_, *args: self.onWellplateChanged(format_))

        self.wellSelectionWidget.signal_wellSelectedPos.connect(self.navigationController.move_to)
        self.wellSelectionWidget.signal_wellSelected.connect(self.multiPointWidget.set_well_selected)

        # camera
        self.camera.set_callback(self.streamHandler.on_new_frame)

        # laser autofocus
        if SUPPORT_LASER_AUTOFOCUS:

            # controllers
            self.configurationManager_focus_camera = core.ConfigurationManager(filename='./focus_camera_configurations.xml')
            self.streamHandler_focus_camera = core.StreamHandler()
            self.liveController_focus_camera = core.LiveController(self.camera_focus,self.microcontroller,self.configurationManager_focus_camera,self,control_illumination=False,for_displacement_measurement=True)
            self.multipointController = core.MultiPointController(self.camera,self.navigationController,self.liveController,self.autofocusController,self.configurationManager,scanCoordinates=self.scanCoordinates,parent=self)
            self.cameraROIWidget.multipoint_controller = self.multipointController # keep the ROI widget pointing at the new controller
            self.imageDisplayWindow_focus = core.ImageDisplayWindow(draw_crosshairs=True)
            self.displacementMeasurementController = core_displacement_measurement.DisplacementMeasurementController()
            self.laserAutofocusController = core.LaserAutofocusController(self.microcontroller,self.camera_focus,self.liveController_focus_camera,self.navigationController,has_two_interfaces=HAS_TWO_INTERFACES,use_glass_top=USE_GLASS_TOP,look_for_cache=False)

            # camera
            self.camera_focus.set_software_triggered_acquisition() #self.camera.set_continuous_acquisition()
            self.camera_focus.set_callback(self.streamHandler_focus_camera.on_new_frame)
            self.camera_focus.enable_callback()
            self.camera_focus.start_streaming()

            # widgets
            if FOCUS_CAMERA_TYPE == "Toupcam":
                self.cameraSettingWidget_focus_camera = widgets.CameraSettingsWidget(self.camera_focus, include_gain_exposure_time = False, include_camera_temperature_setting = True, include_camera_auto_wb_setting = False)
            else:
                self.cameraSettingWidget_focus_camera = widgets.CameraSettingsWidget(self.camera_focus, include_gain_exposure_time = False, include_camera_temperature_setting = False, include_camera_auto_wb_setting = True)

            self.liveControlWidget_focus_camera = widgets.LiveControlWidget(self.streamHandler_focus_camera,self.liveController_focus_camera,self.configurationManager_focus_camera,show_display_options=True)
            self.waveformDisplay = widgets.WaveformDisplay(N=1000,include_x=True,include_y=False)
            self.displacementMeasurementWidget = widgets.DisplacementMeasurementWidget(self.displacementMeasurementController,self.waveformDisplay)
            self.laserAutofocusControlWidget = widgets.LaserAutofocusControlWidget(self.laserAutofocusController)

            self.microscopeControlTabWidget.addTab(self.laserAutofocusControlWidget, "Laser AF")

            dock_laserfocus_image_display = dock.Dock('Focus Camera Image Display', autoOrientation = False)
            dock_laserfocus_image_display.showTitleBar()
            dock_laserfocus_image_display.addWidget(self.imageDisplayWindow_focus.widget)
            dock_laserfocus_image_display.setStretch(x=100,y=100)

            dock_laserfocus_liveController = dock.Dock('Focus Camera Controller', autoOrientation = False)
            dock_laserfocus_liveController.showTitleBar()
            dock_laserfocus_liveController.addWidget(self.liveControlWidget_focus_camera)
            dock_laserfocus_liveController.setStretch(x=100,y=100)
            # dock_laserfocus_liveController.setFixedHeight(self.liveControlWidget_focus_camera.minimumSizeHint().height())
            dock_laserfocus_liveController.setFixedWidth(self.liveControlWidget_focus_camera.minimumSizeHint().width())

            dock_waveform = dock.Dock('Displacement Measurement', autoOrientation = False)
            dock_waveform.showTitleBar()
            dock_waveform.addWidget(self.waveformDisplay)
            dock_waveform.setStretch(x=100,y=40)

            dock_displayMeasurement =  dock.Dock('Displacement Measurement Control', autoOrientation = False)
            dock_displayMeasurement.showTitleBar()
            dock_displayMeasurement.addWidget(self.displacementMeasurementWidget)
            dock_displayMeasurement.setStretch(x=100,y=40)
            dock_displayMeasurement.setFixedWidth(self.displacementMeasurementWidget.minimumSizeHint().width())

            laserfocus_dockArea = dock.DockArea()
            laserfocus_dockArea.addDock(dock_laserfocus_image_display)
            laserfocus_dockArea.addDock(dock_laserfocus_liveController,'right',relativeTo=dock_laserfocus_image_display)
            if SHOW_LEGACY_DISPLACEMENT_MEASUREMENT_WINDOWS:
                laserfocus_dockArea.addDock(dock_waveform,'bottom',relativeTo=dock_laserfocus_liveController)
                laserfocus_dockArea.addDock(dock_displayMeasurement,'bottom',relativeTo=dock_waveform)

            self.imageDisplayTabs.addTab(laserfocus_dockArea,"Laser-Based Focus")

            # connections
            self.liveControlWidget_focus_camera.signal_newExposureTime.connect(self.cameraSettingWidget_focus_camera.set_exposure_time)
            self.liveControlWidget_focus_camera.signal_newAnalogGain.connect(self.cameraSettingWidget_focus_camera.set_analog_gain)
            self.liveControlWidget_focus_camera.update_camera_settings()

            self.streamHandler_focus_camera.signal_new_frame_received.connect(self.liveController_focus_camera.on_new_frame)
            self.streamHandler_focus_camera.image_to_display.connect(self.imageDisplayWindow_focus.display_image)

            self.streamHandler_focus_camera.image_to_display.connect(self.displacementMeasurementController.update_measurement)
            self.displacementMeasurementController.signal_plots.connect(self.waveformDisplay.plot)
            self.displacementMeasurementController.signal_readings.connect(self.displacementMeasurementWidget.display_readings)
            self.laserAutofocusController.image_to_display.connect(self.imageDisplayWindow_focus.display_image)

        # widget for confocal
        if ENABLE_SPINNING_DISK_CONFOCAL:
                self.microscopeControlTabWidget.addTab(self.spinningDiskConfocalWidget,"Confocal")
        if ENABLE_NL5:
            self.microscopeControlTabWidget.addTab(self.nl5Wdiget,"Confocal")

        self.navigationController.move_to_cached_position()

        # Create the menu bar
        menubar = self.menuBar()
        # Add the "Settings" menu
        settings_menu = menubar.addMenu("Settings")
        if SUPPORT_SCIMICROSCOPY_LED_ARRAY:
            # Add the "LED Matrix" action
            led_matrix_action = QAction("LED Matrix", self)
            led_matrix_action.triggered.connect(self.openLedMatrixSettings)
            settings_menu.addAction(led_matrix_action)

    def openLedMatrixSettings(self):
        if SUPPORT_SCIMICROSCOPY_LED_ARRAY:
            dialog = widgets.LedMatrixSettingsDialog(self.liveController.led_array) # to move led_arry outside liveController
            dialog.exec_()

    def onTabChanged(self, index):
        acquisitionWidget = self.recordTabWidget.widget(index)
        if self.wellSelectionWidget.format != 0:
            # originally 'index' (tab 0 = wellplate tab); identify the tab by widget so removing it does not shift this
            self.toggleWellSelector(acquisitionWidget is not self.multiPointWidget)
        try:
            acquisitionWidget.emit_selected_channels()
        except AttributeError:
            pass

    def onWellplateChanged(self, format_):
        if ENABLE_FLEXIBLE_MULTIPOINT:
            self.multiPointWidget2.clear_only_location_list()
        if format_ == 0:
            self.toggleWellSelector(True)
            self.multipointController.inverted_objective = False
            self.navigationController.inverted_objective = False
            self.slidePositionController.setParent(None)
            self.slidePositionController.deleteLater()
            self.slidePositionController = core.SlidePositionController(self.navigationController,self.liveController)
            self.slidePositionController.signal_slide_loading_position_reached.connect(self.navigationWidget.slot_slide_loading_position_reached)
            self.slidePositionController.signal_slide_loading_position_reached.connect(self.multiPointWidget.disable_the_start_aquisition_button)
            self.slidePositionController.signal_slide_scanning_position_reached.connect(self.navigationWidget.slot_slide_scanning_position_reached)
            self.slidePositionController.signal_slide_scanning_position_reached.connect(self.multiPointWidget.enable_the_start_aquisition_button)
            self.slidePositionController.signal_clear_slide.connect(self.navigationViewer.clear_slide)
        else:
            self.toggleWellSelector(False)
            self.multipointController.inverted_objective = True
            self.navigationController.inverted_objective = True
            self.slidePositionController.setParent(None)
            self.slidePositionController.deleteLater()
            self.slidePositionController = core.SlidePositionController(self.navigationController,self.liveController,is_for_wellplate=True)
            self.slidePositionController.signal_slide_loading_position_reached.connect(self.multiPointWidget.disable_the_start_aquisition_button)
            self.slidePositionController.signal_slide_scanning_position_reached.connect(self.navigationWidget.slot_slide_scanning_position_reached)
            self.slidePositionController.signal_slide_scanning_position_reached.connect(self.multiPointWidget.enable_the_start_aquisition_button)
            self.slidePositionController.signal_clear_slide.connect(self.navigationViewer.clear_slide)
            if format_ == 1536:
                self.wellSelectionWidget.setParent(None)
                self.wellSelectionWidget.deleteLater()
                self.wellSelectionWidget = widgets.Well1536SelectionWidget()
                self.scanCoordinates.add_well_selector(self.wellSelectionWidget)
                self.dock_wellSelection.addWidget(self.wellSelectionWidget)
                self.wellSelectionWidget.signal_wellSelectedPos.connect(self.navigationController.move_to)
            elif isinstance(self.wellSelectionWidget, widgets.Well1536SelectionWidget):
                self.wellSelectionWidget.setParent(None)
                self.wellSelectionWidget.deleteLater()
                self.wellSelectionWidget = widgets.WellSelectionWidget(format_)
                self.scanCoordinates.add_well_selector(self.wellSelectionWidget)
                self.dock_wellSelection.addWidget(self.wellSelectionWidget)
                self.wellSelectionWidget.signal_wellSelected.connect(self.multiPointWidget.set_well_selected)
                self.wellSelectionWidget.signal_wellSelectedPos.connect(self.navigationController.move_to)
                self.wellplateFormatWidget.signalWellplateSettings.connect(self.wellSelectionWidget.updateWellplateSettings)

    def toggleWellSelector(self, close):
        self.dock_wellSelection.setVisible(not close)

    def toggleAcquisitionStart(self, acquisition_started):
        current_index = self.recordTabWidget.currentIndex()
        for index in range(self.recordTabWidget.count()):
            self.recordTabWidget.setTabEnabled(index, not acquisition_started or index == current_index)
        if self.recordTabWidget.currentWidget() is self.multiPointWidget and self.wellSelectionWidget.format != 0: # was: current_index == 0 (wellplate tab)
            self.dock_wellSelection.setVisible(not acquisition_started)
        
    # --- input lock during acquisitions (control/input_lock.py)
    def _input_locked_windows(self):
        wins = [self]
        try:
            wins.append(self.multiPointWidget2.towbin_widget)   # Positions window
        except AttributeError:
            pass
        return wins

    def _input_allowed_widgets(self):
        allowed = [getattr(self, 'imageDisplayTabs', None)]
        for w in ('multiPointWidget', 'multiPointWidget2'):
            allowed.append(getattr(getattr(self, w, None), 'btn_startAcquisition', None))
        jw = getattr(self, 'jobsWidget', None)
        if jw is not None:
            allowed += [jw.btn_start, jw.btn_pause, jw.log]
        sw = getattr(self, 'singleMeasurementWidget', None)
        if sw is not None:
            allowed += [sw.btn_abort, sw.log]
        return allowed

    def _on_acquisition_started_lock(self):
        self.inputLock.acquisition_started()
        self.label_acquisition_running.setText('  ACQUISITION RUNNING - controls locked (Stop / Pause available)  ')
        self.label_acquisition_running.setVisible(self.inputLock.enabled)

    def _on_acquisition_finished_lock(self):
        self.inputLock.acquisition_finished()
        self.label_acquisition_running.setVisible(False)

    def _on_job_paused_lock(self, paused):
        self.inputLock.set_paused(paused)
        self.label_acquisition_running.setText('  JOB PAUSED - positions and stage can be changed  ' if paused else
                                               '  ACQUISITION RUNNING - controls locked (Stop / Pause available)  ')

    def show_acquisition_error(self, message):
        """Non-modal, so an unattended computer is not blocked by it."""
        box = QMessageBox(QMessageBox.Warning, 'Acquisition stopped', message, QMessageBox.Ok, self)
        box.setModal(False)
        box.show()
        self._acquisition_error_box = box

    LAYOUT_PREFERENCE_FILE = os.path.join('cache', 'gui_layout.json')

    def _load_two_column_preference(self):
        try:
            with open(self.LAYOUT_PREFERENCE_FILE, 'r') as f:
                return bool(json.load(f).get('two_columns', False))
        except Exception:
            return False

    def _save_two_column_preference(self, two_columns):
        try:
            with open(self.LAYOUT_PREFERENCE_FILE, 'w') as f:
                json.dump({'two_columns': bool(two_columns)}, f)
        except Exception:
            pass # a missing cache folder only means the choice is not remembered

    def _arrange_control_panel(self, two_columns):
        """Put the control panel widgets into one or two columns and give the panel the width of its content."""
        col1, col2, root = self._panel_col1, self._panel_col2, self._panel_root
        # inserting a widget into a layout moves it out of the layout it was in; the stretch stays the last item of each column
        for i, w in enumerate(self._panel_col1_widgets):
            col1.insertWidget(i, w)
        for i, w in enumerate(self._panel_col2_widgets):
            if two_columns:
                col2.insertWidget(i, w)
            else:
                col1.insertWidget(len(self._panel_col1_widgets) + i, w)
        root.setSpacing(10 if two_columns else 0)
        self._panel_two_columns = bool(two_columns)
        cw = self.centralWidget
        cw.setMinimumWidth(0)
        cw.setMaximumWidth(16777215)       # QWIDGETSIZE_MAX: undo the earlier fixed width
        root.invalidate()
        root.activate()
        cw.setFixedWidth(cw.minimumSizeHint().width())

    def set_two_column_panel(self, two_columns):
        """Switch the menus on the right between one and two columns; the image display takes the remaining width."""
        two_columns = bool(two_columns)
        if two_columns == self._panel_two_columns:
            return
        self._arrange_control_panel(two_columns)
        self._save_two_column_preference(two_columns)
        if hasattr(self, 'controlPanel'):
            self.controlPanel.updateGeometry()
            QTimer.singleShot(0, self._fit_control_panel_width)

    def _fit_control_panel_width(self):
        try:
            width = self.controlPanel.sizeHint().width()
            total = sum(self.main_dockArea.topContainer.sizes())
            self.init_control_panel_width(min(width, int(0.75 * total)) if total > 0 else width)   # the image keeps at least a quarter of the window
        except Exception:
            import traceback
            traceback.print_exc()

    def init_control_panel_width(self, width=None):
        """Initial split between image display and controls: the controls get their natural width (the divider can then be dragged)."""
        try:
            width = width or self._control_panel_width
            top = self.main_dockArea.topContainer
            total = sum(top.sizes())
            if total > width:
                top.setSizes([total - width, width])
        except Exception:
            import traceback
            traceback.print_exc()

    def toggleJobPause(self, paused):
        """JOBs: while a job is paused the stage controls and the Flexible Multipoint tab can be used to adjust positions;
        the other acquisition tabs stay locked. On resume everything is locked again."""
        self.navigationWidget.toggle_navigation_controls(not paused)
        if paused:
            for index in range(self.recordTabWidget.count()):
                w = self.recordTabWidget.widget(index)
                self.recordTabWidget.setTabEnabled(index, w is self.multiPointWidget2 or w is self.jobsWidget)
        else:
            self.toggleAcquisitionStart(True)     # locks every tab but the current one (the JOBs tab, where Resume was pressed)

    def closeEvent(self, event):
        # stability: closing during an acquisition left the worker running (moving the stage and reading the camera while
        # the program shut them down) and the processing threads kept the Python process alive (camera/port stay busy)
        mpc = self.multipointController
        if mpc.acquisition_running():
            if not os.environ.get('SQUID_SKIP_CONFIRM'):
                answer = QMessageBox.question(self, 'Acquisition running', 'An acquisition is running.\n\nStop it and close the program?',
                                              QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
            mpc.request_abort_aquisition()
            mpc.job_pause_requested = False
            t0 = time.time()
            while mpc.acquisition_running() and time.time() - t0 < 120:
                QApplication.processEvents()
                time.sleep(0.02)
            QApplication.processEvents()
        self.navigationController.cache_current_position()

        if USE_ZABER_EMISSION_FILTER_WHEEL:
            self.emission_filter_wheel.set_emission_filter(1)
        if USE_OPTOSPIN_EMISSION_FILTER_WHEEL:
            self.emission_filter_wheel.set_emission_filter(1)
            self.emission_filter_wheel.close()

        # move the objective to a defined position upon exit
        self.navigationController.move_x(0.1) # temporary bug fix - move_x needs to be called before move_x_to if the stage has been moved by the joystick
        while self.microcontroller.is_busy():
            time.sleep(0.005)
        self.navigationController.move_x_to(30)
        while self.microcontroller.is_busy():
            time.sleep(0.005)
        self.navigationController.move_y(0.1) # temporary bug fix - move_y needs to be called before move_y_to if the stage has been moved by the joystick
        while self.microcontroller.is_busy():
            time.sleep(0.005)
        self.navigationController.move_y_to(30)
        while self.microcontroller.is_busy():
            time.sleep(0.005)

        self.navigationController.turnoff_axis_pid_control()

        self.liveController.stop_live()
        self.camera.stop_streaming()
        self.camera.close()
        if ENABLE_CELLX:
            for channel in [1,2,3,4]:
                self.cellx.turn_off(channel)
            self.cellx.close()

        self.imageSaver.close()
        self.imageDisplay.close()
        if not SINGLE_WINDOW:
            self.imageDisplayWindow.close()
            self.imageArrayDisplayWindow.close()
            self.tabbedImageDisplayWindow.close()
        if SUPPORT_LASER_AUTOFOCUS:
            self.liveController_focus_camera.stop_live()
            self.camera_focus.close()
            self.imageDisplayWindow_focus.close()
        self.microcontroller.close()

        try:
            self.cswWindow.closeForReal(event)
        except AttributeError:
            pass

        try:
            self.cswfcWindow.closeForReal(event)
        except AttributeError:
            pass

        event.accept()
