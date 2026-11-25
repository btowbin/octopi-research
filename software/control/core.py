# set QT_API environment variable
import os 
import sys
os.environ["QT_API"] = "pyqt5"
import qtpy

# qt libraries
from qtpy.QtCore import *
from qtpy.QtWidgets import *
from qtpy.QtGui import *

from control.processing_handler import ProcessingHandler

import control.utils as utils
import control.autofocus_settings as autofocus_settings
from control._def import *

import control.tracking as tracking
try:
    from control.multipoint_custom_script_entry import *
    print('custom multipoint script found')
except:
    pass
import control.serial_peripherals as serial_peripherals

from queue import Queue
from threading import Thread, Lock
import time
import numpy as np
import pyqtgraph as pg
import scipy
import scipy.signal
import cv2
from datetime import datetime

from lxml import etree as ET
from pathlib import Path
import control.utils_config as utils_config

import math
import json
import pandas as pd

import imageio as iio

import subprocess

# stability (see tests/stress_acquisition.py)
MCU_WAIT_WARN_S = 20    # warn if the microcontroller has not finished a command after this time
MCU_WAIT_ABORT_S = 5    # when Stop was pressed: give up waiting for a command after this time
MCU_WAIT_LIMIT_S = 120  # never wait longer than this for one command (no stage move takes that long)
LOCK_JOYSTICK_DURING_ACQUISITION = True # send SET_JOYSTICK_ENABLED 0/1 at start/end (and pause/resume of JOBs)


class AcquisitionStopped(Exception):
    """The acquisition was ended early for a known reason (message is shown to the user)."""


class ObjectiveStore:
    def __init__(self, objectives_dict=OBJECTIVES, default_objective=DEFAULT_OBJECTIVE):
        self.objectives_dict = objectives_dict
        self.default_objective = default_objective
        self.current_objective = default_objective
        self.tube_lens_mm = TUBE_LENS_MM
        self.sensor_pixel_size_um = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
        self.pixel_size_um = self.calculate_pixel_size(self.current_objective)

    def get_pixel_size(self):
        return self.pixel_size_um

    def calculate_pixel_size(self, objective_name):
        objective = self.objectives_dict[objective_name]
        magnification = objective["magnification"]
        objective_tube_lens_mm = objective["tube_lens_f_mm"]
        pixel_size_um = self.sensor_pixel_size_um / (magnification / (objective_tube_lens_mm / self.tube_lens_mm)) 
        return pixel_size_um

    def set_current_objective(self, objective_name):
        if objective_name in self.objectives_dict:
            self.current_objective = objective_name
            self.pixel_size_um = self.calculate_pixel_size(objective_name)
        else:
            raise ValueError(f"Objective {objective_name} not found in the store.")

    def get_current_objective_info(self):
        return self.objectives_dict[self.current_objective]

class StreamHandler(QObject):

    image_to_display = Signal(np.ndarray)
    packet_image_to_write = Signal(np.ndarray, int, float)
    packet_image_for_tracking = Signal(np.ndarray, int, float)
    signal_new_frame_received = Signal()

    def __init__(self,crop_width=Acquisition.CROP_WIDTH,crop_height=Acquisition.CROP_HEIGHT,display_resolution_scaling=1):
        QObject.__init__(self)
        self.fps_display = 1
        self.fps_save = 1
        self.fps_track = 1
        self.timestamp_last_display = 0
        self.timestamp_last_save = 0
        self.timestamp_last_track = 0

        self.crop_width = crop_width
        self.crop_height = crop_height
        self.display_resolution_scaling = display_resolution_scaling

        self.save_image_flag = False
        self.track_flag = False
        self.handler_busy = False

        # for fps measurement
        self.timestamp_last = 0
        self.counter = 0
        self.fps_real = 0

    def start_recording(self):
        self.save_image_flag = True

    def stop_recording(self):
        self.save_image_flag = False

    def start_tracking(self):
        self.tracking_flag = True

    def stop_tracking(self):
        self.tracking_flag = False

    def set_display_fps(self,fps):
        self.fps_display = fps

    def set_save_fps(self,fps):
        self.fps_save = fps

    def set_crop(self,crop_width,crop_height):
        self.crop_width = crop_width
        self.crop_height = crop_height

    def set_display_resolution_scaling(self, display_resolution_scaling):
        self.display_resolution_scaling = display_resolution_scaling/100
        print(self.display_resolution_scaling)

    def on_new_frame(self, camera):

        if camera.is_live:

            camera.image_locked = True
            self.handler_busy = True
            self.signal_new_frame_received.emit() # self.liveController.turn_off_illumination()

            # measure real fps
            timestamp_now = round(time.time())
            if timestamp_now == self.timestamp_last:
                self.counter = self.counter+1
            else:
                self.timestamp_last = timestamp_now
                self.fps_real = self.counter
                self.counter = 0
                print('real camera fps is ' + str(self.fps_real))

            # moved down (so that it does not modify the camera.current_frame, which causes minor problems for simulation) - 1/30/2022
            # # rotate and flip - eventually these should be done in the camera
            # camera.current_frame = utils.rotate_and_flip_image(camera.current_frame,rotate_image_angle=camera.rotate_image_angle,flip_image=camera.flip_image)

            # crop image
            image_cropped = utils.crop_image(camera.current_frame,self.crop_width,self.crop_height)
            image_cropped = np.squeeze(image_cropped)
            
            # # rotate and flip - moved up (1/10/2022)
            # image_cropped = utils.rotate_and_flip_image(image_cropped,rotate_image_angle=ROTATE_IMAGE_ANGLE,flip_image=FLIP_IMAGE)
            # added on 1/30/2022
            # @@@ to move to camera 
            image_cropped = utils.rotate_and_flip_image(image_cropped,rotate_image_angle=camera.rotate_image_angle,flip_image=camera.flip_image)

            # send image to display
            time_now = time.time()
            if time_now-self.timestamp_last_display >= 1/self.fps_display:
                # self.image_to_display.emit(cv2.resize(image_cropped,(round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling)),cv2.INTER_LINEAR))
                self.image_to_display.emit(utils.crop_image(image_cropped,round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling)))
                self.timestamp_last_display = time_now

            # send image to write
            if self.save_image_flag and time_now-self.timestamp_last_save >= 1/self.fps_save:
                if camera.is_color:
                    image_cropped = cv2.cvtColor(image_cropped,cv2.COLOR_RGB2BGR)
                self.packet_image_to_write.emit(image_cropped,camera.frame_ID,camera.timestamp)
                self.timestamp_last_save = time_now

            # send image to track
            if self.track_flag and time_now-self.timestamp_last_track >= 1/self.fps_track:
                # track is a blocking operation - it needs to be
                # @@@ will cropping before emitting the signal lead to speedup?
                self.packet_image_for_tracking.emit(image_cropped,camera.frame_ID,camera.timestamp)
                self.timestamp_last_track = time_now

            self.handler_busy = False
            camera.image_locked = False

    '''
    def on_new_frame_from_simulation(self,image,frame_ID,timestamp):
        # check whether image is a local copy or pointer, if a pointer, needs to prevent the image being modified while this function is being executed
        
        self.handler_busy = True

        # crop image
        image_cropped = utils.crop_image(image,self.crop_width,self.crop_height)

        # send image to display
        time_now = time.time()
        if time_now-self.timestamp_last_display >= 1/self.fps_display:
            self.image_to_display.emit(cv2.resize(image_cropped,(round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling)),cv2.INTER_LINEAR))
            self.timestamp_last_display = time_now

        # send image to write
        if self.save_image_flag and time_now-self.timestamp_last_save >= 1/self.fps_save:
            self.packet_image_to_write.emit(image_cropped,frame_ID,timestamp)
            self.timestamp_last_save = time_now

        # send image to track
        if time_now-self.timestamp_last_display >= 1/self.fps_track:
            # track emit
            self.timestamp_last_track = time_now

        self.handler_busy = False
    '''

class ImageSaver(QObject):

    stop_recording = Signal()

    def __init__(self,image_format=Acquisition.IMAGE_FORMAT):
        QObject.__init__(self)
        self.base_path = './'
        self.experiment_ID = ''
        self.image_format = image_format
        self.max_num_image_per_folder = 1000
        self.queue = Queue(10) # max 10 items in the queue
        self.image_lock = Lock()
        self.stop_signal_received = False
        self.thread = Thread(target=self.process_queue)
        self.thread.start()
        self.counter = 0
        self.recording_start_time = 0
        self.recording_time_limit = -1

    def process_queue(self):
        while True:
            # stop the thread if stop signal is received
            if self.stop_signal_received:
                return
            # process the queue
            try:
                [image,frame_ID,timestamp] = self.queue.get(timeout=0.1)
                self.image_lock.acquire(True)
                folder_ID = int(self.counter/self.max_num_image_per_folder)
                file_ID = int(self.counter%self.max_num_image_per_folder)
                # create a new folder
                if file_ID == 0:
                    os.mkdir(os.path.join(self.base_path,self.experiment_ID,str(folder_ID)))

                if image.dtype == np.uint16:
                    # need to use tiff when saving 16 bit images
                    saving_path = os.path.join(self.base_path,self.experiment_ID,str(folder_ID),str(file_ID) + '_' + str(frame_ID) + '.tiff')
                    iio.imwrite(saving_path,image)
                else:
                    saving_path = os.path.join(self.base_path,self.experiment_ID,str(folder_ID),str(file_ID) + '_' + str(frame_ID) + '.' + self.image_format)
                    cv2.imwrite(saving_path,image)

                self.counter = self.counter + 1
                self.queue.task_done()
                self.image_lock.release()
            except:
                pass
                            
    def enqueue(self,image,frame_ID,timestamp):
        try:
            self.queue.put_nowait([image,frame_ID,timestamp])
            if ( self.recording_time_limit>0 ) and ( time.time()-self.recording_start_time >= self.recording_time_limit ):
                self.stop_recording.emit()
            # when using self.queue.put(str_), program can be slowed down despite multithreading because of the block and the GIL
        except:
            print('imageSaver queue is full, image discarded')

    def set_base_path(self,path):
        self.base_path = path

    def set_recording_time_limit(self,time_limit):
        self.recording_time_limit = time_limit

    def start_new_experiment(self,experiment_ID,add_timestamp=True):
        if add_timestamp:
            # generate unique experiment ID
            self.experiment_ID = experiment_ID + '_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')
        else:
            self.experiment_ID = experiment_ID
        self.recording_start_time = time.time()
        # create a new folder
        try:
            os.mkdir(os.path.join(self.base_path,self.experiment_ID))
            # to do: save configuration
        except:
            pass
        # reset the counter
        self.counter = 0

    def close(self):
        self.queue.join()
        self.stop_signal_received = True
        self.thread.join()


class ImageSaver_Tracking(QObject):
    def __init__(self,base_path,image_format='bmp'):
        QObject.__init__(self)
        self.base_path = base_path
        self.image_format = image_format
        self.max_num_image_per_folder = 1000
        self.queue = Queue(100) # max 100 items in the queue
        self.image_lock = Lock()
        self.stop_signal_received = False
        self.thread = Thread(target=self.process_queue)
        self.thread.start()

    def process_queue(self):
        while True:
            # stop the thread if stop signal is received
            if self.stop_signal_received:
                return
            # process the queue
            try:
                [image,frame_counter,postfix] = self.queue.get(timeout=0.1)
                self.image_lock.acquire(True)
                folder_ID = int(frame_counter/self.max_num_image_per_folder)
                file_ID = int(frame_counter%self.max_num_image_per_folder)
                # create a new folder
                if file_ID == 0:
                    os.mkdir(os.path.join(self.base_path,str(folder_ID)))
                if image.dtype == np.uint16:
                    saving_path = os.path.join(self.base_path,str(folder_ID),str(file_ID) + '_' + str(frame_counter) + '_' + postfix + '.tiff')
                    iio.imwrite(saving_path,image)
                else:
                    saving_path = os.path.join(self.base_path,str(folder_ID),str(file_ID) + '_' + str(frame_counter) + '_' + postfix + '.' + self.image_format)
                    cv2.imwrite(saving_path,image)
                self.queue.task_done()
                self.image_lock.release()
            except:
                pass
                            
    def enqueue(self,image,frame_counter,postfix):
        try:
            self.queue.put_nowait([image,frame_counter,postfix])
        except:
            print('imageSaver queue is full, image discarded')

    def close(self):
        self.queue.join()
        self.stop_signal_received = True
        self.thread.join()


'''
class ImageSaver_MultiPointAcquisition(QObject):
'''

class ImageDisplay(QObject):

    image_to_display = Signal(np.ndarray)

    def __init__(self):
        QObject.__init__(self)
        self.queue = Queue(10) # max 10 items in the queue
        self.image_lock = Lock()
        self.stop_signal_received = False
        self.thread = Thread(target=self.process_queue)
        self.thread.start()        
        
    def process_queue(self):
        while True:
            # stop the thread if stop signal is received
            if self.stop_signal_received:
                return
            # process the queue
            try:
                [image,frame_ID,timestamp] = self.queue.get(timeout=0.1)
                self.image_lock.acquire(True)
                self.image_to_display.emit(image)
                self.image_lock.release()
                self.queue.task_done()
            except:
                pass

    # def enqueue(self,image,frame_ID,timestamp):
    def enqueue(self,image):
        try:
            self.queue.put_nowait([image,None,None])
            # when using self.queue.put(str_) instead of try + nowait, program can be slowed down despite multithreading because of the block and the GIL
            pass
        except:
            print('imageDisplay queue is full, image discarded')

    def emit_directly(self,image):
        self.image_to_display.emit(image)

    def close(self):
        self.queue.join()
        self.stop_signal_received = True
        self.thread.join()

class Configuration:
    def __init__(self,mode_id=None,name=None,color=None,camera_sn=None,exposure_time=None,analog_gain=None,illumination_source=None,illumination_intensity=None,z_offset=None,pixel_format=None,_pixel_format_options=None,emission_filter_position=None):
        self.id = mode_id
        self.name = name
        self.color = color
        self.exposure_time = exposure_time
        self.analog_gain = analog_gain
        self.illumination_source = illumination_source
        self.illumination_intensity = illumination_intensity
        self.camera_sn = camera_sn
        self.z_offset = z_offset
        self.pixel_format = pixel_format
        if self.pixel_format is None:
            self.pixel_format = "default"
        self._pixel_format_options = _pixel_format_options
        if _pixel_format_options is None:
            self._pixel_format_options = self.pixel_format
        self.emission_filter_position = emission_filter_position

class LiveController(QObject):
    
    def __init__(self,camera,microcontroller,configurationManager,parent=None,control_illumination=True,use_internal_timer_for_hardware_trigger=True,for_displacement_measurement=False):
        QObject.__init__(self)
        self.microscope = parent
        self.camera = camera
        self.microcontroller = microcontroller
        self.configurationManager = configurationManager
        self.currentConfiguration = None
        self.trigger_mode = TriggerMode.SOFTWARE # @@@ change to None
        self.is_live = False
        self.control_illumination = control_illumination
        self.illumination_on = False
        self.use_internal_timer_for_hardware_trigger = use_internal_timer_for_hardware_trigger # use QTimer vs timer in the MCU
        self.for_displacement_measurement = for_displacement_measurement
        if hasattr(camera, 'exposure_lag_fix'):   # FLIR: pulse for the 'Empty frame in between' option in hardware trigger mode (no illumination)
            camera.dummy_trigger = lambda: self.microcontroller.send_hardware_trigger(control_illumination=False, illumination_on_time_us=0)

        self.fps_trigger = 1;
        self.timer_trigger_interval = (1/self.fps_trigger)*1000

        self.timer_trigger = QTimer()
        self.timer_trigger.setInterval(int(self.timer_trigger_interval))
        self.timer_trigger.timeout.connect(self.trigger_acquisition)

        self.trigger_ID = -1

        self.fps_real = 0
        self.counter = 0
        self.timestamp_last = 0

        self.display_resolution_scaling = DEFAULT_DISPLAY_CROP/100

        self.enable_channel_auto_filter_switching = True

        if USE_LDI_SERIAL_CONTROL:
            self.ldi = self.microscope.ldi
      
        if SUPPORT_SCIMICROSCOPY_LED_ARRAY:
            # to do: add error handling
            self.led_array = serial_peripherals.SciMicroscopyLEDArray(SCIMICROSCOPY_LED_ARRAY_SN,SCIMICROSCOPY_LED_ARRAY_DISTANCE,SCIMICROSCOPY_LED_ARRAY_TURN_ON_DELAY)
            self.led_array.set_NA(SCIMICROSCOPY_LED_ARRAY_DEFAULT_NA)

    # illumination control
    def turn_on_illumination(self):
        if USE_LDI_SERIAL_CONTROL and 'Fluorescence' in self.currentConfiguration.name and LDI_SHUTTER_MODE == 'PC':
            self.ldi.set_active_channel_shutter(1)
        elif SUPPORT_SCIMICROSCOPY_LED_ARRAY and 'LED matrix' in self.currentConfiguration.name:
            self.led_array.turn_on_illumination()
        else:
            self.microcontroller.turn_on_illumination()
        self.illumination_on = True

    def turn_off_illumination(self):
        if USE_LDI_SERIAL_CONTROL and 'Fluorescence' in self.currentConfiguration.name and LDI_SHUTTER_MODE == 'PC':
            self.ldi.set_active_channel_shutter(0)
        elif SUPPORT_SCIMICROSCOPY_LED_ARRAY and 'LED matrix' in self.currentConfiguration.name:
            self.led_array.turn_off_illumination()
        else:
            self.microcontroller.turn_off_illumination()
        self.illumination_on = False

    def set_illumination(self,illumination_source,intensity,update_channel_settings=True):
        if illumination_source < 10: # LED matrix
            if SUPPORT_SCIMICROSCOPY_LED_ARRAY:
                # set color
                if 'BF LED matrix full_R' in self.currentConfiguration.name:
                    self.led_array.set_color((1,0,0))
                elif 'BF LED matrix full_G' in self.currentConfiguration.name:
                    self.led_array.set_color((0,1,0))
                elif 'BF LED matrix full_B' in self.currentConfiguration.name:
                    self.led_array.set_color((0,0,1))
                else:
                    self.led_array.set_color(SCIMICROSCOPY_LED_ARRAY_DEFAULT_COLOR)
                # set intensity
                self.led_array.set_brightness(intensity)
                # set mode
                if 'BF LED matrix left half' in self.currentConfiguration.name:
                    self.led_array.set_illumination('dpc.l')
                if 'BF LED matrix right half' in self.currentConfiguration.name:
                    self.led_array.set_illumination('dpc.r')
                if 'BF LED matrix top half' in self.currentConfiguration.name:
                    self.led_array.set_illumination('dpc.t')
                if 'BF LED matrix bottom half' in self.currentConfiguration.name:
                    self.led_array.set_illumination('dpc.b')
                if 'BF LED matrix full' in self.currentConfiguration.name:
                    self.led_array.set_illumination('bf')
                if 'DF LED matrix' in self.currentConfiguration.name:
                    self.led_array.set_illumination('df')
            else:
                self.microcontroller.set_illumination_led_matrix(illumination_source,r=(intensity/100)*LED_MATRIX_R_FACTOR,g=(intensity/100)*LED_MATRIX_G_FACTOR,b=(intensity/100)*LED_MATRIX_B_FACTOR)
        else:
            # update illumination
            if USE_LDI_SERIAL_CONTROL and 'Fluorescence' in self.currentConfiguration.name:
                if LDI_SHUTTER_MODE == 'PC':
                    # set LDI active channel
                    print('set active channel to ' + str(illumination_source))
                    self.ldi.set_active_channel(int(illumination_source))
                if LDI_INTENSITY_MODE == 'PC':
                    if update_channel_settings:
                        # set intensity for active channel
                        print('set intensity')
                        self.ldi.set_intensity(int(illumination_source),intensity)
            elif ENABLE_NL5 and NL5_USE_DOUT and 'Fluorescence' in self.currentConfiguration.name:
                wavelength = int(self.currentConfiguration.name[13:16])
                self.microscope.nl5.set_active_channel(NL5_WAVENLENGTH_MAP[wavelength])
                if NL5_USE_AOUT and update_channel_settings:
                    self.microscope.nl5.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength],int(intensity))
                if ENABLE_CELLX:
                    self.microscope.cellx.set_laser_power(NL5_WAVENLENGTH_MAP[wavelength],int(intensity))
            else:
                self.microcontroller.set_illumination(illumination_source,intensity)

        # set emission filter position
        if ENABLE_SPINNING_DISK_CONFOCAL:
            try:
                self.microscope.xlight.set_emission_filter(XLIGHT_EMISSION_FILTER_MAPPING[illumination_source],extraction=False,validate=XLIGHT_VALIDATE_WHEEL_POS)
            except Exception as e:
                print('not setting emission filter position due to ' + str(e))

        if USE_ZABER_EMISSION_FILTER_WHEEL and self.enable_channel_auto_filter_switching:
            try:
                if self.currentConfiguration.emission_filter_position != self.microscope.emission_filter_wheel.current_index:
                    if ZABER_EMISSION_FILTER_WHEEL_BLOCKING_CALL:
                        self.microscope.emission_filter_wheel.set_emission_filter(self.currentConfiguration.emission_filter_position,blocking=True)
                    else:
                        self.microscope.emission_filter_wheel.set_emission_filter(self.currentConfiguration.emission_filter_position,blocking=False)
                        if self.trigger_mode == TriggerMode.SOFTWARE:
                            time.sleep(ZABER_EMISSION_FILTER_WHEEL_DELAY_MS/1000)
                        else:
                            time.sleep(max(0,ZABER_EMISSION_FILTER_WHEEL_DELAY_MS/1000-self.camera.strobe_delay_us/1e6))
            except Exception as e:
                print('not setting emission filter position due to ' + str(e))

        if USE_OPTOSPIN_EMISSION_FILTER_WHEEL and self.enable_channel_auto_filter_switching and OPTOSPIN_EMISSION_FILTER_WHEEL_TTL_TRIGGER == False:
            try:
                if self.currentConfiguration.emission_filter_position != self.microscope.emission_filter_wheel.current_index:
                    self.microscope.emission_filter_wheel.set_emission_filter(self.currentConfiguration.emission_filter_position)
                    if self.trigger_mode == TriggerMode.SOFTWARE:
                        time.sleep(OPTOSPIN_EMISSION_FILTER_WHEEL_DELAY_MS/1000)
                    elif self.trigger_mode == TriggerMode.HARDWARE:
                        time.sleep(max(0,OPTOSPIN_EMISSION_FILTER_WHEEL_DELAY_MS/1000-self.camera.strobe_delay_us/1e6))
            except Exception as e:
                print('not setting emission filter position due to ' + str(e))

    def start_live(self):
        # optional check before live view starts (set by CameraROIWidget): the hardware ROI must be centred.
        # Done first, while is_live is still False, so no live frame is handled while the ROI is corrected.
        hook = getattr(self, 'before_live_hook', None)
        if hook is not None:
            try:
                hook()
            except Exception as e:
                print('*** camera ROI check before live view failed: ' + str(e))
        self.is_live = True
        self.camera.is_live = True
        self.camera.start_streaming()
        if self.trigger_mode == TriggerMode.SOFTWARE or ( self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger ):
            self.camera.enable_callback() # in case it's disabled e.g. by the laser AF controller
            self._start_triggerred_acquisition()
        # if controlling the laser displacement measurement camera
        if self.for_displacement_measurement:
            self.microcontroller.set_pin_level(MCU_PINS.AF_LASER,1)

    def stop_live(self):
        if self.is_live:
            self.is_live = False
            self.camera.is_live = False
            if hasattr(self.camera,'stop_exposure'):
                self.camera.stop_exposure()
            if self.trigger_mode == TriggerMode.SOFTWARE:
                self._stop_triggerred_acquisition()
            # self.camera.stop_streaming() # 20210113 this line seems to cause problems when using af with multipoint
            if self.trigger_mode == TriggerMode.CONTINUOUS:
                self.camera.stop_streaming()
            if ( self.trigger_mode == TriggerMode.SOFTWARE ) or ( self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger ):
                self._stop_triggerred_acquisition()
            if self.control_illumination:
                self.turn_off_illumination()
            # if controlling the laser displacement measurement camera
            if self.for_displacement_measurement:
                self.microcontroller.set_pin_level(MCU_PINS.AF_LASER,0)

    # software trigger related
    def trigger_acquisition(self):
        if self.trigger_mode == TriggerMode.SOFTWARE:
            if self.control_illumination and self.illumination_on == False:
                self.turn_on_illumination()
            self.trigger_ID = self.trigger_ID + 1
            self.camera.send_trigger()
            # measure real fps
            timestamp_now = round(time.time())
            if timestamp_now == self.timestamp_last:
                self.counter = self.counter+1
            else:
                self.timestamp_last = timestamp_now
                self.fps_real = self.counter
                self.counter = 0
                # print('real trigger fps is ' + str(self.fps_real))
        elif self.trigger_mode == TriggerMode.HARDWARE:
            self.trigger_ID = self.trigger_ID + 1
            if ENABLE_NL5 and NL5_USE_DOUT:
                self.microscope.nl5.start_acquisition()
            else:
                self.microcontroller.send_hardware_trigger(control_illumination=True,illumination_on_time_us=self.camera.exposure_time*1000)

    def _start_triggerred_acquisition(self):
        self.timer_trigger.start()

    def _set_trigger_fps(self,fps_trigger):
        self.fps_trigger = fps_trigger
        self.timer_trigger_interval = (1/self.fps_trigger)*1000
        self.timer_trigger.setInterval(int(self.timer_trigger_interval))

    def _stop_triggerred_acquisition(self):
        self.timer_trigger.stop()

    # trigger mode and settings
    def set_trigger_mode(self,mode):
        if mode == TriggerMode.SOFTWARE:
            if self.is_live and ( self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger ):
                self._stop_triggerred_acquisition()
            self.camera.set_software_triggered_acquisition()
            if self.is_live:
                self._start_triggerred_acquisition()
        if mode == TriggerMode.HARDWARE:
            if self.trigger_mode == TriggerMode.SOFTWARE and self.is_live:
                self._stop_triggerred_acquisition()
            # self.camera.reset_camera_acquisition_counter()
            self.camera.set_hardware_triggered_acquisition()
            self.microcontroller.set_strobe_delay_us(int(self.camera.strobe_delay_us))
            self._strobe_delay_sent = int(self.camera.strobe_delay_us)
            if self.is_live and self.use_internal_timer_for_hardware_trigger:
                self._start_triggerred_acquisition()
        if mode == TriggerMode.CONTINUOUS: 
            if ( self.trigger_mode == TriggerMode.SOFTWARE ) or ( self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger ):
                self._stop_triggerred_acquisition()
            self.camera.set_continuous_acquisition()
        self.trigger_mode = mode

    def set_trigger_fps(self,fps):
        if ( self.trigger_mode == TriggerMode.SOFTWARE ) or ( self.trigger_mode == TriggerMode.HARDWARE and self.use_internal_timer_for_hardware_trigger ):
            self._set_trigger_fps(fps)
    
    # set microscope mode
    # @@@ to do: change softwareTriggerGenerator to TriggerGeneratror
    def set_microscope_mode(self,configuration):

        self.currentConfiguration = configuration
        print("setting microscope mode to " + self.currentConfiguration.name)
        
        # temporarily stop live while changing mode
        if self.is_live is True:
            self.timer_trigger.stop()
            if self.control_illumination:
                self.turn_off_illumination()

        # set camera exposure time and analog gain
        self.camera.set_exposure_time(self.currentConfiguration.exposure_time)
        self.camera.set_analog_gain(self.currentConfiguration.analog_gain)
        self._update_strobe_delay()

        # set illumination
        if self.control_illumination:
            self.set_illumination(self.currentConfiguration.illumination_source,self.currentConfiguration.illumination_intensity)

        # restart live 
        if self.is_live is True:
            if self.control_illumination:
                self.turn_on_illumination()
            self.timer_trigger.start()

    def _update_strobe_delay(self):
        """towbinlab: in hardware-trigger mode the microcontroller waits strobe_delay_us after the trigger before it
        switches the light on. The camera's value changes with pixel format and ROI (measured rolling-shutter
        readout); before, it was only sent when the trigger mode was switched, so it could be stale."""
        if self.trigger_mode != TriggerMode.HARDWARE:
            return
        try:
            delay = int(self.camera.strobe_delay_us)
        except (AttributeError, TypeError, ValueError):
            return
        if delay != getattr(self, '_strobe_delay_sent', None):
            self.microcontroller.set_strobe_delay_us(delay)
            self._strobe_delay_sent = delay

    def get_trigger_mode(self):
        return self.trigger_mode

    # slot
    def on_new_frame(self):
        if self.fps_trigger <= 5:
            if self.control_illumination and self.illumination_on == True:
                self.turn_off_illumination()

    def set_display_resolution_scaling(self, display_resolution_scaling):
        self.display_resolution_scaling = display_resolution_scaling/100

class NavigationController(QObject):

    xPos = Signal(float)
    yPos = Signal(float)
    zPos = Signal(float)
    thetaPos = Signal(float)
    xyPos = Signal(float,float)
    signal_joystick_button_pressed = Signal()

    # x y z axis pid enable flag
    pid_enable_flag = [False, False, False]


    def __init__(self,microcontroller, objectivestore, parent=None):
        # parent should be set to OctopiGUI instance to enable updates
        # to camera settings, e.g. binning, that would affect click-to-move
        QObject.__init__(self)
        self.microcontroller = microcontroller
        self.parent = parent
        self.objectiveStore = objectivestore
        self.x_pos_mm = 0
        self.y_pos_mm = 0
        self.z_pos_mm = 0
        self.z_pos = 0
        self.theta_pos_rad = 0
        self.x_microstepping = MICROSTEPPING_DEFAULT_X
        self.y_microstepping = MICROSTEPPING_DEFAULT_Y
        self.z_microstepping = MICROSTEPPING_DEFAULT_Z
        self.click_to_move = False
        self.theta_microstepping = MICROSTEPPING_DEFAULT_THETA
        self.enable_joystick_button_action = True

        # to be moved to gui for transparency
        self.microcontroller.set_callback(self.update_pos)

        # self.timer_read_pos = QTimer()
        # self.timer_read_pos.setInterval(PosUpdate.INTERVAL_MS)
        # self.timer_read_pos.timeout.connect(self.update_pos)
        # self.timer_read_pos.start()

        # scan start position
        self.scan_begin_position_x = 0
        self.scan_begin_position_y = 0

    def set_flag_click_to_move(self, flag):
        self.click_to_move = flag

    def get_flag_click_to_move(self):
        return self.click_to_move

    def scan_preview_move_from_click(self, click_x, click_y, image_width, image_height, Nx=1, Ny=1, dx_mm=0.9, dy_mm=0.9):
        """
        napariTiledDisplay uses the Nx, Ny, dx_mm, dy_mm fields to move to the correct fov first
        imageArrayDisplayWindow assumes only a single fov (default values do not impact calculation but this is less correct)
        """
        # check if click to move enabled
        if not self.click_to_move:
            print("allow click to move")
            return
        # restore to raw coordicate
        click_x = image_width / 2.0 + click_x
        click_y = image_height / 2.0 - click_y
        print("click - (x, y):", (click_x, click_y))
        fov_col = click_x * Nx // image_width
        fov_row = click_y * Ny // image_height
        print("image - (col, row):", (fov_col, fov_row))
        end_position_x = Ny % 2 # right side or left side
        fov_col = Nx - (fov_col + 1) if end_position_x else fov_col
        fov_row = fov_row
        print("fov - (col, row):", fov_col, fov_row)

        pixel_sign_x = (-1)**end_position_x # inverted
        pixel_sign_y = -1 if INVERTED_OBJECTIVE else 1
        print("pixel_sign_x, pixel_sign_y", pixel_sign_x, pixel_sign_y)
 
        # move to selected fov
        self.move_x_to(self.scan_begin_position_x+dx_mm*fov_col*pixel_sign_x)
        self.microcontroller.wait_till_operation_is_completed()
        self.move_y_to(self.scan_begin_position_y+dy_mm*fov_row*pixel_sign_y)
        self.microcontroller.wait_till_operation_is_completed()
        # move to actual click, offset from center fov
        tile_width = (image_width / Nx) * PRVIEW_DOWNSAMPLE_FACTOR
        tile_height = (image_height / Ny) * PRVIEW_DOWNSAMPLE_FACTOR
        offset_x = (click_x * PRVIEW_DOWNSAMPLE_FACTOR) % tile_width
        offset_y = (click_y * PRVIEW_DOWNSAMPLE_FACTOR) % tile_height
        offset_x_centered = int(offset_x - tile_width / 2)
        offset_y_centered = int(tile_height / 2 - offset_y)
        self.move_from_click(offset_x_centered, offset_y_centered, tile_width, tile_height)

    def move_from_click(self, click_x, click_y, image_width, image_height):
        if self.click_to_move:
            pixel_size_um = self.objectiveStore.pixel_size_um
            #pixel_binning_x, pixel_binning_y = self.get_pixel_binning()
            #pixel_size_x = pixel_size_um * pixel_binning_x
            #pixel_size_y = pixel_size_um * pixel_binning_y

            pixel_sign_x = 1
            pixel_sign_y = 1 if INVERTED_OBJECTIVE else -1

            #delta_x = pixel_sign_x * pixel_size_x * click_x / 1000.0
            #delta_y = pixel_sign_y * pixel_size_y * click_y / 1000.0
            delta_x = pixel_sign_x * pixel_size_um * click_x / 1000.0
            delta_y = pixel_sign_y * pixel_size_um * click_y / 1000.0

            if not IS_HCS:
                delta_x /= 2
                delta_y /= 2

            self.move_x(delta_x)
            self.microcontroller.wait_till_operation_is_completed()
            self.move_y(delta_y)
            self.microcontroller.wait_till_operation_is_completed()

    def get_pixel_binning(self):
        try:
            highest_res = max(self.parent.camera.res_list, key=lambda res: res[0] * res[1])
            resolution = self.parent.camera.resolution
            pixel_binning_x = max(1, highest_res[0] / resolution[0])
            pixel_binning_y = max(1, highest_res[1] / resolution[1])
        except AttributeError:
            pixel_binning_x = 1
            pixel_binning_y = 1
        return pixel_binning_x, pixel_binning_y

    def move_to_cached_position(self):
        if not os.path.isfile("cache/last_coords.txt"):
            return
        with open("cache/last_coords.txt","r") as f:
            for line in f:
                try:
                    x,y,z = line.strip("\n").strip().split(",")
                    x = float(x)
                    y = float(y)
                    z = float(z)
                    self.move_to(x,y)
                    self.move_z_to(z)
                    break
                except:
                    pass
                break

    def cache_current_position(self):
        with open("cache/last_coords.txt","w") as f:
            f.write(",".join([str(self.x_pos_mm),str(self.y_pos_mm),str(self.z_pos_mm)]))

    def move_x(self,delta):
        self.microcontroller.move_x_usteps(int(delta/(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))))

    def move_y(self,delta):
        self.microcontroller.move_y_usteps(int(delta/(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))))

    def move_z(self,delta):
        self.microcontroller.move_z_usteps(int(delta/(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))))

    def move_x_to(self,delta):
        self.microcontroller.move_x_to_usteps(STAGE_MOVEMENT_SIGN_X*int(delta/(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))))

    def move_y_to(self,delta):
        self.microcontroller.move_y_to_usteps(STAGE_MOVEMENT_SIGN_Y*int(delta/(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))))

    def move_z_to(self,delta):
        self.microcontroller.move_z_to_usteps(STAGE_MOVEMENT_SIGN_Z*int(delta/(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))))

    def move_x_usteps(self,usteps):
        self.microcontroller.move_x_usteps(usteps)

    def move_y_usteps(self,usteps):
        self.microcontroller.move_y_usteps(usteps)

    def move_z_usteps(self,usteps):
        self.microcontroller.move_z_usteps(usteps)

    def update_pos(self,microcontroller):
        # get position from the microcontroller
        x_pos, y_pos, z_pos, theta_pos = microcontroller.get_pos()
        self.z_pos = z_pos
        # calculate position in mm or rad
        if USE_ENCODER_X:
            self.x_pos_mm = x_pos*ENCODER_POS_SIGN_X*ENCODER_STEP_SIZE_X_MM
        else:
            self.x_pos_mm = x_pos*STAGE_POS_SIGN_X*(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))
        if USE_ENCODER_Y:
            self.y_pos_mm = y_pos*ENCODER_POS_SIGN_Y*ENCODER_STEP_SIZE_Y_MM
        else:
            self.y_pos_mm = y_pos*STAGE_POS_SIGN_Y*(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))
        if USE_ENCODER_Z:
            self.z_pos_mm = z_pos*ENCODER_POS_SIGN_Z*ENCODER_STEP_SIZE_Z_MM
        else:
            self.z_pos_mm = z_pos*STAGE_POS_SIGN_Z*(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))
        if USE_ENCODER_THETA:
            self.theta_pos_rad = theta_pos*ENCODER_POS_SIGN_THETA*ENCODER_STEP_SIZE_THETA
        else:
            self.theta_pos_rad = theta_pos*STAGE_POS_SIGN_THETA*(2*math.pi/(self.theta_microstepping*FULLSTEPS_PER_REV_THETA))
        # emit the updated position
        self.xPos.emit(self.x_pos_mm)
        self.yPos.emit(self.y_pos_mm)
        self.zPos.emit(self.z_pos_mm*1000)
        self.thetaPos.emit(self.theta_pos_rad*360/(2*math.pi))
        self.xyPos.emit(self.x_pos_mm,self.y_pos_mm)

        if microcontroller.signal_joystick_button_pressed_event:
            if self.enable_joystick_button_action:
                self.signal_joystick_button_pressed.emit()
            print('joystick button pressed')
            microcontroller.signal_joystick_button_pressed_event = False

    def home_x(self):
        self.microcontroller.home_x()

    def home_y(self):
        self.microcontroller.home_y()

    def home_z(self):
        self.microcontroller.home_z()

    def home_theta(self):
        self.microcontroller.home_theta()

    def home_xy(self):
        self.microcontroller.home_xy()

    def zero_x(self):
        self.microcontroller.zero_x()

    def zero_y(self):
        self.microcontroller.zero_y()

    def zero_z(self):
        self.microcontroller.zero_z()

    def zero_theta(self):
        self.microcontroller.zero_tehta()

    def home(self):
        pass

    def set_x_limit_pos_mm(self,value_mm):
        if STAGE_MOVEMENT_SIGN_X > 0:
            self.microcontroller.set_lim(LIMIT_CODE.X_POSITIVE,int(value_mm/(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))))
        else:
            self.microcontroller.set_lim(LIMIT_CODE.X_NEGATIVE,STAGE_MOVEMENT_SIGN_X*int(value_mm/(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))))

    def set_x_limit_neg_mm(self,value_mm):
        if STAGE_MOVEMENT_SIGN_X > 0:
            self.microcontroller.set_lim(LIMIT_CODE.X_NEGATIVE,int(value_mm/(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))))
        else:
            self.microcontroller.set_lim(LIMIT_CODE.X_POSITIVE,STAGE_MOVEMENT_SIGN_X*int(value_mm/(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))))

    def set_y_limit_pos_mm(self,value_mm):
        if STAGE_MOVEMENT_SIGN_Y > 0:
            self.microcontroller.set_lim(LIMIT_CODE.Y_POSITIVE,int(value_mm/(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))))
        else:
            self.microcontroller.set_lim(LIMIT_CODE.Y_NEGATIVE,STAGE_MOVEMENT_SIGN_Y*int(value_mm/(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))))

    def set_y_limit_neg_mm(self,value_mm):
        if STAGE_MOVEMENT_SIGN_Y > 0:
            self.microcontroller.set_lim(LIMIT_CODE.Y_NEGATIVE,int(value_mm/(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))))
        else:
            self.microcontroller.set_lim(LIMIT_CODE.Y_POSITIVE,STAGE_MOVEMENT_SIGN_Y*int(value_mm/(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))))

    def set_z_limit_pos_mm(self,value_mm):
        if STAGE_MOVEMENT_SIGN_Z > 0:
            self.microcontroller.set_lim(LIMIT_CODE.Z_POSITIVE,int(value_mm/(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))))
        else:
            self.microcontroller.set_lim(LIMIT_CODE.Z_NEGATIVE,STAGE_MOVEMENT_SIGN_Z*int(value_mm/(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))))

    def set_z_limit_neg_mm(self,value_mm):
        if STAGE_MOVEMENT_SIGN_Z > 0:
            self.microcontroller.set_lim(LIMIT_CODE.Z_NEGATIVE,int(value_mm/(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))))
        else:
            self.microcontroller.set_lim(LIMIT_CODE.Z_POSITIVE,STAGE_MOVEMENT_SIGN_Z*int(value_mm/(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))))
    
    def move_to(self,x_mm,y_mm):
        self.move_x_to(x_mm)
        self.move_y_to(y_mm)

    def configure_encoder(self, axis, transitions_per_revolution,flip_direction):
        self.microcontroller.configure_stage_pid(axis, transitions_per_revolution=int(transitions_per_revolution), flip_direction=flip_direction)

    def set_pid_control_enable(self, axis, enable_flag):
        self.pid_enable_flag[axis] = enable_flag;
        if self.pid_enable_flag[axis] is True:
            self.microcontroller.turn_on_stage_pid(axis)
        else:
            self.microcontroller.turn_off_stage_pid(axis)

    def turnoff_axis_pid_control(self):
        for i in range(len(self.pid_enable_flag)):
            if self.pid_enable_flag[i] is True:
                self.microcontroller.turn_off_stage_pid(i)

    def get_pid_control_flag(self, axis):
        return self.pid_enable_flag[axis]

    def keep_scan_begin_position(self, x, y):
        self.scan_begin_position_x = x
        self.scan_begin_position_y = y

    def set_axis_PID_arguments(self, axis, pid_p, pid_i, pid_d):
        self.microcontroller.set_pid_arguments(axis, pid_p, pid_i, pid_d)

class SlidePositionControlWorker(QObject):
    
    finished = Signal()
    signal_stop_live = Signal()
    signal_resume_live = Signal()

    def __init__(self,slidePositionController,home_x_and_y_separately=False):
        QObject.__init__(self)
        self.slidePositionController = slidePositionController
        self.navigationController = slidePositionController.navigationController
        self.microcontroller = self.navigationController.microcontroller
        self.liveController = self.slidePositionController.liveController
        self.home_x_and_y_separately = home_x_and_y_separately

    def wait_till_operation_is_completed(self,timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S):
        while self.microcontroller.is_busy():
            time.sleep(SLEEP_TIME_S)
            if time.time() - timestamp_start > SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S:
                print('Error - slide position switching timeout, the program will exit')
                self.navigationController.move_x(0)
                self.navigationController.move_y(0)
                sys.exit(1)

    def move_to_slide_loading_position(self):
        was_live = self.liveController.is_live
        if was_live:
            self.signal_stop_live.emit()

        # retract z
        timestamp_start = time.time()
        self.slidePositionController.z_pos = self.navigationController.z_pos # zpos at the beginning of the scan
        self.navigationController.move_z_to(OBJECTIVE_RETRACTED_POS_MM)
        self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
        print('z retracted')
        self.slidePositionController.objective_retracted = True
        
        # move to position
        # for well plate
        if self.slidePositionController.is_for_wellplate:
            # reset limits
            self.navigationController.set_x_limit_pos_mm(100)
            self.navigationController.set_x_limit_neg_mm(-100)
            self.navigationController.set_y_limit_pos_mm(100)
            self.navigationController.set_y_limit_neg_mm(-100)
            # home for the first time
            if self.slidePositionController.homing_done == False:
                print('running homing first')
                timestamp_start = time.time()
                # x needs to be at > + 20 mm when homing y
                self.navigationController.move_x(20)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                # home y
                self.navigationController.home_y()
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.zero_y()
                # home x
                self.navigationController.home_x()
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.zero_x()
                self.slidePositionController.homing_done = True
            # homing done previously
            else:
                timestamp_start = time.time()
                self.navigationController.move_x_to(20)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.move_y_to(SLIDE_POSITION.LOADING_Y_MM)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.move_x_to(SLIDE_POSITION.LOADING_X_MM)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
            # set limits again
            self.navigationController.set_x_limit_pos_mm(SOFTWARE_POS_LIMIT.X_POSITIVE)
            self.navigationController.set_x_limit_neg_mm(SOFTWARE_POS_LIMIT.X_NEGATIVE)
            self.navigationController.set_y_limit_pos_mm(SOFTWARE_POS_LIMIT.Y_POSITIVE)
            self.navigationController.set_y_limit_neg_mm(SOFTWARE_POS_LIMIT.Y_NEGATIVE)
        else:

            # for glass slide
            if self.slidePositionController.homing_done == False or SLIDE_POTISION_SWITCHING_HOME_EVERYTIME:
                if self.home_x_and_y_separately:
                    timestamp_start = time.time()
                    self.navigationController.home_x()
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.zero_x()
                    self.navigationController.move_x(SLIDE_POSITION.LOADING_X_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.home_y()
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.zero_y()
                    self.navigationController.move_y(SLIDE_POSITION.LOADING_Y_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                else:
                    timestamp_start = time.time()
                    self.navigationController.home_xy()
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.zero_x()
                    self.navigationController.zero_y()
                    self.navigationController.move_x(SLIDE_POSITION.LOADING_X_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.move_y(SLIDE_POSITION.LOADING_Y_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.slidePositionController.homing_done = True
            else:
                timestamp_start = time.time()
                self.navigationController.move_y(SLIDE_POSITION.LOADING_Y_MM-self.navigationController.y_pos_mm)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.move_x(SLIDE_POSITION.LOADING_X_MM-self.navigationController.x_pos_mm)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)

        if was_live:
            self.signal_resume_live.emit()

        self.slidePositionController.slide_loading_position_reached = True
        self.finished.emit()

    def move_to_slide_scanning_position(self):
        was_live = self.liveController.is_live
        if was_live:
            self.signal_stop_live.emit()

        # move to position
        # for well plate
        if self.slidePositionController.is_for_wellplate:
            # home for the first time
            if self.slidePositionController.homing_done == False:
                timestamp_start = time.time()

                # x needs to be at > + 20 mm when homing y
                self.navigationController.move_x(20)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                # home y
                self.navigationController.home_y()
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.zero_y()
                # home x
                self.navigationController.home_x()
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.zero_x()
                self.slidePositionController.homing_done = True
                # move to scanning position
                self.navigationController.move_x_to(SLIDE_POSITION.SCANNING_X_MM)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)

                self.navigationController.move_y_to(SLIDE_POSITION.SCANNING_Y_MM)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                   
            else:
                timestamp_start = time.time()
                self.navigationController.move_x_to(SLIDE_POSITION.SCANNING_X_MM)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.move_y_to(SLIDE_POSITION.SCANNING_Y_MM)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
        else:
            if self.slidePositionController.homing_done == False or SLIDE_POTISION_SWITCHING_HOME_EVERYTIME:
                if self.home_x_and_y_separately:
                    timestamp_start = time.time()
                    self.navigationController.home_y()
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.zero_y()
                    self.navigationController.move_y(SLIDE_POSITION.SCANNING_Y_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.home_x()
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.zero_x()
                    self.navigationController.move_x(SLIDE_POSITION.SCANNING_X_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                else:
                    timestamp_start = time.time()
                    self.navigationController.home_xy()
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.zero_x()
                    self.navigationController.zero_y()
                    self.navigationController.move_y(SLIDE_POSITION.SCANNING_Y_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                    self.navigationController.move_x(SLIDE_POSITION.SCANNING_X_MM)
                    self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.slidePositionController.homing_done = True
            else:
                timestamp_start = time.time()
                self.navigationController.move_y(SLIDE_POSITION.SCANNING_Y_MM-self.navigationController.y_pos_mm)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.move_x(SLIDE_POSITION.SCANNING_X_MM-self.navigationController.x_pos_mm)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)

        # restore z
        if self.slidePositionController.objective_retracted:
            if self.navigationController.get_pid_control_flag(2) is False:
                _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                self.navigationController.microcontroller.move_z_to_usteps(self.slidePositionController.z_pos - STAGE_MOVEMENT_SIGN_Z*_usteps_to_clear_backlash)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
                self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
            else:
                self.navigationController.microcontroller.move_z_to_usteps(self.slidePositionController.z_pos)
                self.wait_till_operation_is_completed(timestamp_start, SLIDE_POTISION_SWITCHING_TIMEOUT_LIMIT_S)
            self.slidePositionController.objective_retracted = False
            print('z position restored')
        
        if was_live:
            self.signal_resume_live.emit()

        self.slidePositionController.slide_scanning_position_reached = True
        self.finished.emit()

class SlidePositionController(QObject):

    signal_slide_loading_position_reached = Signal()
    signal_slide_scanning_position_reached = Signal()
    signal_clear_slide = Signal()

    def __init__(self,navigationController,liveController,is_for_wellplate=False):
        QObject.__init__(self)
        self.navigationController = navigationController
        self.liveController = liveController
        self.slide_loading_position_reached = False
        self.slide_scanning_position_reached = False
        self.homing_done = False
        self.is_for_wellplate = is_for_wellplate
        self.retract_objective_before_moving = RETRACT_OBJECTIVE_BEFORE_MOVING_TO_LOADING_POSITION
        self.objective_retracted = False
        self.thread = None

    def move_to_slide_loading_position(self):
        # create a QThread object
        self.thread = QThread()
        # create a worker object
        self.slidePositionControlWorker = SlidePositionControlWorker(self)
        # move the worker to the thread
        self.slidePositionControlWorker.moveToThread(self.thread)
        # connect signals and slots
        self.thread.started.connect(self.slidePositionControlWorker.move_to_slide_loading_position)
        self.slidePositionControlWorker.signal_stop_live.connect(self.slot_stop_live,type=Qt.BlockingQueuedConnection)
        self.slidePositionControlWorker.signal_resume_live.connect(self.slot_resume_live,type=Qt.BlockingQueuedConnection)
        self.slidePositionControlWorker.finished.connect(self.signal_slide_loading_position_reached.emit)
        self.slidePositionControlWorker.finished.connect(self.slidePositionControlWorker.deleteLater)
        self.slidePositionControlWorker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.thread.quit)
        # self.slidePositionControlWorker.finished.connect(self.threadFinished,type=Qt.BlockingQueuedConnection)
        # start the thread
        self.thread.start()

    def move_to_slide_scanning_position(self):
    	# create a QThread object
        self.thread = QThread()
        # create a worker object
        self.slidePositionControlWorker = SlidePositionControlWorker(self)
        # move the worker to the thread
        self.slidePositionControlWorker.moveToThread(self.thread)
        # connect signals and slots
        self.thread.started.connect(self.slidePositionControlWorker.move_to_slide_scanning_position)
        self.slidePositionControlWorker.signal_stop_live.connect(self.slot_stop_live,type=Qt.BlockingQueuedConnection)
        self.slidePositionControlWorker.signal_resume_live.connect(self.slot_resume_live,type=Qt.BlockingQueuedConnection)
        self.slidePositionControlWorker.finished.connect(self.signal_slide_scanning_position_reached.emit)
        self.slidePositionControlWorker.finished.connect(self.slidePositionControlWorker.deleteLater)
        self.slidePositionControlWorker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.thread.quit)
        # self.slidePositionControlWorker.finished.connect(self.threadFinished,type=Qt.BlockingQueuedConnection)
        # start the thread
        print('before thread.start()')
        self.thread.start()
        self.signal_clear_slide.emit()

    def slot_stop_live(self):
        self.liveController.stop_live()

    def slot_resume_live(self):
        self.liveController.start_live()

    # def threadFinished(self):
    # 	print('========= threadFinished ========= ')

class AutofocusWorker(QObject):

    finished = Signal()
    image_to_display = Signal(np.ndarray)
    # signal_current_configuration = Signal(Configuration)

    def __init__(self,autofocusController):
        QObject.__init__(self)
        self.autofocusController = autofocusController

        self.camera = self.autofocusController.camera
        self.microcontroller = self.autofocusController.navigationController.microcontroller
        self.navigationController = self.autofocusController.navigationController
        self.liveController = self.autofocusController.liveController

        self.N = self.autofocusController.N
        self.deltaZ = self.autofocusController.deltaZ
        self.deltaZ_usteps = self.autofocusController.deltaZ_usteps
        self.focus_measure_name = self.autofocusController.focus_measure
        
        self.crop_width = self.autofocusController.crop_width
        self.crop_height = self.autofocusController.crop_height

    def run(self):
        # stability: always emit finished (else autofocus_in_progress stays True and a multipoint acquisition waits forever)
        try:
            self.run_autofocus()
        except BaseException:
            import traceback
            traceback.print_exc()
            print('*** autofocus failed (see above); z is left where it is')
            self.autofocusController.last_error = traceback.format_exc().strip().splitlines()[-1]
            try:
                self.liveController.turn_off_illumination()
            except BaseException:
                pass
        finally:
            self.finished.emit()

    def wait_till_operation_is_completed(self):
        t0 = time.time()
        while self.microcontroller.is_busy():
            time.sleep(SLEEP_TIME_S)
            if time.time() - t0 > MCU_WAIT_LIMIT_S:
                raise AcquisitionStopped('autofocus: the stage did not finish a move within ' + str(int(MCU_WAIT_LIMIT_S)) + ' s')

    def run_autofocus(self):
        # @@@ to add: increase gain, decrease exposure time
        # @@@ can move the execution into a thread - done 08/21/2021
        focus_measure_vs_z = [0]*self.N
        focus_measure_max = 0

        z_af_offset_usteps = self.deltaZ_usteps*round(self.N/2)
        # self.navigationController.move_z_usteps(-z_af_offset_usteps) # combine with the back and forth maneuver below
        # self.wait_till_operation_is_completed()

        # maneuver for achiving uniform step size and repeatability when using open-loop control
        # can be moved to the firmware
        if self.navigationController.get_pid_control_flag(2) is False:
            _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
            self.navigationController.move_z_usteps(-_usteps_to_clear_backlash-z_af_offset_usteps)
            self.wait_till_operation_is_completed()
            self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
            self.wait_till_operation_is_completed()
        else:
            self.navigationController.move_z_usteps(-z_af_offset_usteps)
            self.wait_till_operation_is_completed()

        steps_moved = 0
        metric_failed = False
        for i in range(self.N):
            self.navigationController.move_z_usteps(self.deltaZ_usteps)
            self.wait_till_operation_is_completed()
            steps_moved = steps_moved + 1
            # trigger acquisition (including turning on the illumination) and read frame
            if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                self.liveController.turn_on_illumination()
                self.wait_till_operation_is_completed()
                self.camera.send_trigger()
                image = self.camera.read_frame()
            elif self.liveController.trigger_mode == TriggerMode.HARDWARE:
                # towbinlab fix: 'config' was never defined in this worker, so every autofocus in hardware-trigger mode
                # raised a NameError before the trigger was sent (no light, autofocus failed)
                current_configuration = self.liveController.currentConfiguration
                if ENABLE_NL5 and NL5_USE_DOUT and current_configuration is not None and 'Fluorescence' in current_configuration.name:
                    self.camera.image_is_ready = False # to remove
                    self.liveController.microscope.nl5.start_acquisition()
                    image = self.camera.read_frame(reset_image_ready_flag=False)
                else:
                    self.microcontroller.send_hardware_trigger(control_illumination=True,illumination_on_time_us=self.camera.exposure_time*1000)
                    image = self.camera.read_frame()
            if image is None:
                if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                    self.liveController.turn_off_illumination() # stability: do not leave the light on
                continue
            # tunr of the illumination if using software trigger
            if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                self.liveController.turn_off_illumination()
            image = utils.crop_image(image,self.crop_width,self.crop_height)
            image = utils.rotate_and_flip_image(image,rotate_image_angle=self.camera.rotate_image_angle,flip_image=self.camera.flip_image)
            self.image_to_display.emit(image)
            QApplication.processEvents()
            timestamp_0 = time.time()
            focus_measure = self.autofocusController.compute_focus_measure(image,self.focus_measure_name)
            if focus_measure is None:
                # a custom focus measure raised: finish the scan without evaluating it and return to the start z
                metric_failed = True
                focus_measure = 0
            timestamp_1 = time.time()
            print('             calculating focus measure took ' + str(timestamp_1-timestamp_0) + ' second')
            focus_measure_vs_z[i] = focus_measure
            print(i,focus_measure)
            focus_measure_max = max(focus_measure, focus_measure_max)
            if (not metric_failed) and focus_measure < focus_measure_max*AF.STOP_THRESHOLD:
                break

        if metric_failed:
            print('*** autofocus: focus measure failed, z position is left unchanged ***')
            focus_measure_vs_z = [0]*self.N
            focus_measure_vs_z[max(0,min(round(self.N/2)-1,steps_moved-1))] = 1

        QApplication.processEvents()

        # move to the starting location
        # self.navigationController.move_z_usteps(-steps_moved*self.deltaZ_usteps) # combine with the back and forth maneuver below
        # self.wait_till_operation_is_completed()

        # maneuver for achiving uniform step size and repeatability when using open-loop control
        if self.navigationController.get_pid_control_flag(2) is False:
            _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
            self.navigationController.move_z_usteps(-_usteps_to_clear_backlash-steps_moved*self.deltaZ_usteps)
            # determine the in-focus position
            idx_in_focus = focus_measure_vs_z.index(max(focus_measure_vs_z))
            self.wait_till_operation_is_completed()
            self.navigationController.move_z_usteps(_usteps_to_clear_backlash+(idx_in_focus+1)*self.deltaZ_usteps)
            self.wait_till_operation_is_completed()
        else:
            # determine the in-focus position
            idx_in_focus = focus_measure_vs_z.index(max(focus_measure_vs_z))
            self.navigationController.move_z_usteps((idx_in_focus+1)*self.deltaZ_usteps-steps_moved*self.deltaZ_usteps)
            self.wait_till_operation_is_completed()

        QApplication.processEvents()

        # move to the calculated in-focus position
        # self.navigationController.move_z_usteps(idx_in_focus*self.deltaZ_usteps)
        # self.wait_till_operation_is_completed() # combine with the movement above
        if idx_in_focus == 0:
            print('moved to the bottom end of the AF range')
        if idx_in_focus == self.N-1:
            print('moved to the top end of the AF range')

class AutoFocusController(QObject):

    z_pos = Signal(float)
    autofocusFinished = Signal()
    image_to_display = Signal(np.ndarray)
    settings_changed = Signal() # emitted whenever N, deltaZ, focus measure or autofocus channel change
    signal_set_configuration = Signal(object) # single autofocus tests: switch the live channel (connected to the live control in gui_hcs)

    def __init__(self,camera,navigationController,liveController):
        QObject.__init__(self)
        self.camera = camera
        self.navigationController = navigationController
        self.liveController = liveController
        self._single_af_restore = None # channel to put back after a single autofocus test (see autofocus_single)
        self.autofocusFinished.connect(self._restore_configuration_after_single_af)
        self.N = None
        self.deltaZ = None
        self.deltaZ_usteps = None
        self.crop_width = AF.CROP_WIDTH
        self.crop_height = AF.CROP_HEIGHT
        self.focus_measure = FOCUS_MEASURE_OPERATOR # 'LAPE', 'GLVA' or 'CUSTOM:<file>::<function>'
        self.af_channel = MULTIPOINT_AUTOFOCUS_CHANNEL # channel used for autofocus in multipoint acquisitions
        # when the autofocus runs during a multipoint acquisition (default = at every time point, as before)
        self.af_schedule_mode = 'every_timepoint' # 'every_timepoint', 'every_n' or 'staggered'
        self.af_schedule_n = 1
        self.af_all_at_t0 = True # staggered mode: autofocus all positions at the first time point (t=0)
        self.autofocus_in_progress = False
        self.focus_map_coords = []
        self.use_focus_map = False

    def set_N(self,N):
        self.N = N
        self.settings_changed.emit()

    def set_deltaZ(self,deltaZ_um):
        mm_per_ustep_Z = SCREW_PITCH_Z_MM/(self.navigationController.z_microstepping*FULLSTEPS_PER_REV_Z)
        self.deltaZ = deltaZ_um/1000
        self.deltaZ_usteps = round((deltaZ_um/1000)/mm_per_ustep_Z)
        self.settings_changed.emit()

    def set_focus_measure(self,name):
        self.focus_measure = name
        self.settings_changed.emit()

    def set_af_channel(self,channel_name):
        self.af_channel = channel_name
        self.settings_changed.emit()

    def set_af_schedule(self,mode=None,n=None,all_at_t0=None):
        if mode is not None:
            if mode not in ('every_timepoint','every_n','staggered'):
                raise ValueError('unknown autofocus schedule mode: ' + str(mode))
            self.af_schedule_mode = mode
        if n is not None:
            self.af_schedule_n = max(1,int(n))
        if all_at_t0 is not None:
            self.af_all_at_t0 = bool(all_at_t0)
        self.settings_changed.emit()

    def autofocus_scheduled(self,time_point,position_index):
        """Is the contrast autofocus to be run for this position at this time point of a multipoint acquisition?

        time_point: 0 for the first time point (folder '0'); position_index: 0 for the first position of the list.
        every_timepoint: always.   every_n: at t = 0, n, 2n, ... for all positions.
        staggered: position p (counted from 0) at the time points t (counted from 0) with (p - t) divisible by n,
        i.e. for n=10 t=0: positions 0, 10, 20, ...; t=1: positions 1, 11, 21, ...; optionally all positions at t=0.
        """
        n = max(1,int(self.af_schedule_n))
        if self.af_schedule_mode == 'every_n':
            return time_point % n == 0
        if self.af_schedule_mode == 'staggered':
            if self.af_all_at_t0 and time_point == 0:
                return True
            return (position_index - time_point) % n == 0  # time points and positions both counted from 0
        return True

    def compute_focus_measure(self,image,name=None):
        """Focus measure of one frame; None if a custom focus measure raised an exception."""
        try:
            return autofocus_settings.REGISTRY.calculate(image,self.focus_measure if name is None else name)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print('*** focus measure failed: ' + str(e))
            return None

    def get_settings(self):
        """Current autofocus conditions as a dict (what is written to a settings file)."""
        return {
            'focus_measure': self.focus_measure,
            'af_channel': self.af_channel,
            'delta_z_um': round(self.deltaZ*1000,6) if self.deltaZ is not None else None,
            'n_planes': self.N,
            'af_schedule_mode': self.af_schedule_mode,
            'af_schedule_n': self.af_schedule_n,
            'af_all_at_t0': self.af_all_at_t0,
        }

    def set_crop(self,crop_width,crop_height):
        self.crop_width = crop_width
        self.crop_height = crop_height

    def _configuration_by_name(self,name):
        try:
            return next((c for c in self.liveController.configurationManager.configurations if c.name == name),None)
        except Exception:
            return None

    def autofocus_single(self,*args):
        """towbinlab: one autofocus started by hand (Autofocus buttons, joystick button). It uses the autofocus channel
        of the Contrast AF tab (its illumination, intensity, exposure and gain) - not the channel that happens to be
        selected in the live control, as before. The previous live channel is put back when the autofocus has ended.
        (Autofocus inside a multipoint acquisition already switched to the autofocus channel itself.)"""
        cfg = self._configuration_by_name(self.af_channel)
        current = self.liveController.currentConfiguration
        self._single_af_restore = None
        if self.use_focus_map:
            pass # no images are taken: z is interpolated
        elif cfg is None:
            print('*** autofocus channel "' + str(self.af_channel) + '" not found in the channel list: autofocus uses the live channel')
        elif current is None or current.name != cfg.name:
            self._single_af_restore = current
            self.signal_set_configuration.emit(cfg)
        self.autofocus(False)
        if not self.autofocus_in_progress: # nothing was started (e.g. the thread refused): put the channel back at once
            self._restore_configuration_after_single_af()

    def _restore_configuration_after_single_af(self):
        prev, self._single_af_restore = self._single_af_restore, None
        if prev is not None:
            self.signal_set_configuration.emit(prev)

    def autofocus(self, focus_map_override=False):
        if self.use_focus_map and (not focus_map_override):
            self.autofocus_in_progress = True
            self.navigationController.microcontroller.wait_till_operation_is_completed()
            x = self.navigationController.x_pos_mm
            y = self.navigationController.y_pos_mm
            
            # z here is in mm because that's how the navigation controller stores it
            target_z = utils.interpolate_plane(*self.focus_map_coords[:3], (x,y))
            print(f"Interpolated target z as {target_z} mm from focus map, moving there.")
            self.navigationController.move_z_to(target_z)
            self.navigationController.microcontroller.wait_till_operation_is_completed()
            self.autofocus_in_progress = False
            self.autofocusFinished.emit()
            return
        # stop live
        if self.liveController.is_live:
            self.was_live_before_autofocus = True
            self.liveController.stop_live()
        else:
            self.was_live_before_autofocus = False

        # temporarily disable call back -> image does not go through streamHandler
        if self.camera.callback_is_enabled:
            self.callback_was_enabled_before_autofocus = True
            self.camera.disable_callback()
        else:
            self.callback_was_enabled_before_autofocus = False

        self.autofocus_in_progress = True

        # create a QThread object
        try:
            if self.thread.isRunning():
                # stability: do not terminate() it (a running autofocus may hold the camera or the serial port).
                # Usually the previous autofocus has finished and only its thread is still shutting down (its quit()
                # is queued to a thread that is busy): stop that thread directly and wait for it.
                self.thread.quit()
                if not self.thread.wait(15000):
                    print('*** autofocus is still running, the new request is ignored ***')
                    return
        except (RuntimeError, AttributeError):
            pass
        old = self.__dict__.get('thread')
        if isinstance(old, QThread):
            self._old_threads = [t for t in getattr(self, '_old_threads', []) if not t.isFinished()] + [old]
        self.thread = QThread()
        # create a worker object
        self.autofocusWorker = AutofocusWorker(self)
        # move the worker to the thread
        self.autofocusWorker.moveToThread(self.thread)
        # connect signals and slots
        self.thread.started.connect(self.autofocusWorker.run)
        self.autofocusWorker.finished.connect(self._on_autofocus_completed)
        self.autofocusWorker.finished.connect(self.autofocusWorker.deleteLater)
        self.autofocusWorker.finished.connect(self.thread.quit)
        self.autofocusWorker.image_to_display.connect(self.slot_image_to_display)
        # self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.thread.quit)
        # start the thread
        self.thread.start()
        
    def _on_autofocus_completed(self):
        # towbinlab: single autofocus test: put the previous live channel back before live view restarts
        self._restore_configuration_after_single_af()
        # re-enable callback
        if self.callback_was_enabled_before_autofocus:
            self.camera.enable_callback()
        
        # re-enable live if it's previously on
        if self.was_live_before_autofocus:
            self.liveController.start_live()

        # emit the autofocus finished signal to enable the UI
        self.autofocusFinished.emit()
        QApplication.processEvents()
        print('autofocus finished')

        # update the state
        self.autofocus_in_progress = False

    def slot_image_to_display(self,image):
        self.image_to_display.emit(image)

    def wait_till_autofocus_has_completed(self):
        while self.autofocus_in_progress == True:
            QApplication.processEvents()
            time.sleep(0.005)
        print('autofocus wait has completed, exit wait')

    def set_focus_map_use(self, enable):
        if not enable:
            print("Disabling focus map.")
            self.use_focus_map = False
            return
        if len(self.focus_map_coords) < 3:
            print("Not enough coordinates (less than 3) for focus map generation, disabling focus map.")
            self.use_focus_map = False
            return
        x1,y1,_ = self.focus_map_coords[0]
        x2,y2,_ = self.focus_map_coords[1]
        x3,y3,_ = self.focus_map_coords[2]

        detT = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if detT == 0:
            print("Your 3 x-y coordinates are linear, cannot use to interpolate, disabling focus map.")
            self.use_focus_map = False
            return

        if enable:
            print("Enabling focus map.")
            self.use_focus_map = True

    def clear_focus_map(self):
        self.focus_map_coords = []
        self.set_focus_map_use(False)

    def gen_focus_map(self, coord1,coord2,coord3):
        """
        Navigate to 3 coordinates and get your focus-map coordinates
        by autofocusing there and saving the z-values.
        :param coord1-3: Tuples of (x,y) values, coordinates in mm.
        :raise: ValueError if coordinates are all on the same line
        """
        x1,y1 = coord1
        x2,y2 = coord2
        x3,y3 = coord3
        detT = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if detT == 0:
            raise ValueError("Your 3 x-y coordinates are linear")
        
        self.focus_map_coords = []

        for coord in [coord1,coord2,coord3]:
            print(f"Navigating to coordinates ({coord[0]},{coord[1]}) to sample for focus map")
            self.navigationController.move_to(coord[0],coord[1])
            self.navigationController.microcontroller.wait_till_operation_is_completed()
            print("Autofocusing")
            self.autofocus(True)
            self.wait_till_autofocus_has_completed()
            #self.navigationController.microcontroller.wait_till_operation_is_completed()
            x = self.navigationController.x_pos_mm
            y = self.navigationController.y_pos_mm
            z = self.navigationController.z_pos_mm
            print(f"Adding coordinates ({x},{y},{z}) to focus map")
            self.focus_map_coords.append((x,y,z))

        print("Generated focus map.")

    def add_current_coords_to_focus_map(self):
        if len(self.focus_map_coords) >= 3:
            print("Replacing last coordinate on focus map.")
        self.navigationController.microcontroller.wait_till_operation_is_completed()
        print("Autofocusing")
        self.autofocus(True)
        self.wait_till_autofocus_has_completed()
        #self.navigationController.microcontroller.wait_till_operation_is_completed()
        x = self.navigationController.x_pos_mm
        y = self.navigationController.y_pos_mm
        z = self.navigationController.z_pos_mm
        if len(self.focus_map_coords) >= 2:
            x1,y1,_ = self.focus_map_coords[0]
            x2,y2,_ = self.focus_map_coords[1]
            x3 = x
            y3 = y

            detT = (y2-y3) * (x1-x3) + (x3-x2) * (y1-y3)
            if detT == 0:
                raise ValueError("Your 3 x-y coordinates are linear. Navigate to a different coordinate or clear and try again.")
        if len(self.focus_map_coords) >= 3:
            self.focus_map_coords.pop()
        self.focus_map_coords.append((x,y,z))
        print(f"Added triple ({x},{y},{z}) to focus map")


class MultiPointWorker(QObject):

    finished = Signal()
    image_to_display = Signal(np.ndarray)
    spectrum_to_display = Signal(np.ndarray)
    image_to_display_multi = Signal(np.ndarray,int)
    image_to_display_tiled_preview = Signal(np.ndarray)
    signal_current_configuration = Signal(Configuration)
    signal_register_current_fov = Signal(float,float)
    signal_detection_stats = Signal(object)
    signal_z_piezo_um = Signal(float)
    napari_layers_update = Signal(np.ndarray, int, int, int, str)
    napari_layers_init = Signal(int, int, object, bool)

    signal_update_stats = Signal(object)
    signal_job_message = Signal(str)   # JOBs: progress messages of a job script
    signal_job_status = Signal(object) # JOBs: live status (dicts, see control/jobs_live.py)
    signal_acquisition_error = Signal(str) # stability: an error ended the acquisition (message for the user)
    signal_update_z = Signal(int,float)    # towbinlab: (position index, z in mm) after an autofocus, for the Positions table (GUI thread)
    signal_flex_status = Signal(object)    # towbinlab: progress messages for the Flexible Multipoint live view (same format as the JOBs, see control/jobs_live.py)

    def __init__(self,multiPointController):
        QObject.__init__(self)
        self.multiPointController = multiPointController

        self.signal_update_stats.connect(self.update_stats)
        self.start_time = 0
        self.processingHandler = multiPointController.processingHandler
        self.camera = self.multiPointController.camera
        self.microcontroller = self.multiPointController.microcontroller
        self.usb_spectrometer = self.multiPointController.usb_spectrometer
        self.navigationController = self.multiPointController.navigationController
        self.liveController = self.multiPointController.liveController
        self.autofocusController = self.multiPointController.autofocusController
        self.configurationManager = self.multiPointController.configurationManager
        self.NX = self.multiPointController.NX
        self.NY = self.multiPointController.NY
        self.NZ = self.multiPointController.NZ
        self.Nt = self.multiPointController.Nt
        self.deltaX = self.multiPointController.deltaX
        self.deltaX_usteps = self.multiPointController.deltaX_usteps
        self.deltaY = self.multiPointController.deltaY
        self.deltaY_usteps = self.multiPointController.deltaY_usteps
        self.deltaZ = self.multiPointController.deltaZ
        self.deltaZ_usteps = self.multiPointController.deltaZ_usteps
        self.dt = self.multiPointController.deltat
        self.do_autofocus = self.multiPointController.do_autofocus
        self.do_reflection_af= self.multiPointController.do_reflection_af
        self.crop_width = self.multiPointController.crop_width
        self.crop_height = self.multiPointController.crop_height
        self.display_resolution_scaling = self.multiPointController.display_resolution_scaling
        self.counter = self.multiPointController.counter
        self.experiment_ID = self.multiPointController.experiment_ID
        self.base_path = self.multiPointController.base_path
        self.selected_configurations = self.multiPointController.selected_configurations
        self.use_piezo = self.multiPointController.use_piezo
        self.detection_stats = {}
        self.async_detection_stats = {}
        self.timestamp_acquisition_started = self.multiPointController.timestamp_acquisition_started
        self.time_point = 0

        self.microscope = self.multiPointController.parent

        self.t_dpc = []
        self.t_inf = []
        self.t_over=[]

        self.tiled_preview = None
        

    def update_stats(self, new_stats):
        for k in new_stats.keys():
            try:
                self.detection_stats[k]+=new_stats[k]
            except:
                self.detection_stats[k] = 0
                self.detection_stats[k]+=new_stats[k]
        if "Total RBC" in self.detection_stats and "Total Positives" in self.detection_stats:
            self.detection_stats["Positives per 5M RBC"] = 5e6*(self.detection_stats["Total Positives"]/self.detection_stats["Total RBC"])
        self.signal_detection_stats.emit(self.detection_stats)

    # --- towbinlab: progress messages for the live view and the position log -----------------------------------------
    def _job_time(self):
        return time.time() - self.timestamp_acquisition_started

    def _flex_notify(self, msg):
        msg.setdefault('t', self._job_time())
        try:
            self.signal_flex_status.emit(msg)
        except Exception:
            pass # the live view must never break an acquisition

    def _flex_state(self, state, **kw):
        self._flex_notify(dict(type='state', state=state, tp=self.time_point, **kw))

    def _flex_done(self, kind, point, channel=None):
        self._flex_notify(dict(type='done', kind=kind, point=point, tp=self.time_point, channel=channel))

    POSITION_LOG_COLUMNS = ['time','time_point','position','id','event','x_list_mm','y_list_mm','z_list_um','x_stage_mm','y_stage_mm',
                            'z_stage_um','z_before_af_um','z_widget_um','z_original_um','note']

    def _open_position_log(self):
        """position_log.csv in the experiment folder: where every position is according to the positions list and the stage,
        before and after each autofocus, so that it can be checked that the focused z is used at the next time point."""
        import csv
        self._position_log_path = os.path.join(self.base_path,self.experiment_ID,'position_log.csv')
        try:
            with open(self._position_log_path,'w',newline='',encoding='utf-8') as f:
                csv.writer(f).writerow(self.POSITION_LOG_COLUMNS)
        except Exception as e:
            print('position log could not be created: ' + str(e))
            self._position_log_path = None

    def _log_position(self, event, coordinate_id, note='', z_before_af_mm=None):
        path = getattr(self,'_position_log_path',None)
        if path is None:
            return
        import csv
        def um(z_mm):
            return '' if z_mm is None else round(float(z_mm)*1000,3)
        def mm(v):
            return '' if v is None else round(float(v),5)
        try:
            row = self.scan_coordinates_mm[coordinate_id]
            z_list = row[2] if len(row) == 3 else None
            z_orig = self.scan_coordinates_mm_backup[coordinate_id][2] if len(row) == 3 else None
            try:
                z_widget = self.microscope.multiPointWidget2.location_list[coordinate_id,2]
            except Exception:
                z_widget = None
            try:
                position_id = self.microscope.multiPointWidget2.location_ids[coordinate_id]
            except Exception:
                position_id = ''
            nav = self.navigationController
            with open(path,'a',newline='',encoding='utf-8') as f:
                csv.writer(f).writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], self.time_point, coordinate_id, position_id, event,
                                        mm(row[0]), mm(row[1]), um(z_list), mm(nav.x_pos_mm), mm(nav.y_pos_mm), um(nav.z_pos_mm),
                                        um(z_before_af_mm), um(z_widget), um(z_orig), note])
        except Exception as e:
            print('position log: ' + str(e)) # never break the acquisition

    def run_job_script(self):
        """JOBs: run the job file selected in the JOBs tab instead of the standard loops (see control/jobs)."""
        self.start_time = time.perf_counter_ns()
        if self.camera.is_streaming == False:
            self.camera.start_streaming()
        from control.jobs.runner import run_job
        from control.jobs.hardware_backend import HardwareBackend
        mpc = self.multiPointController
        try:
            result = run_job(mpc.job_file, mpc.job_points, HardwareBackend(self),
                             experiment_dir=os.path.join(self.base_path,self.experiment_ID),
                             meta=dict(experiment_id=self.experiment_ID), observer=self.signal_job_status.emit,
                             save_as=getattr(mpc, 'job_save_as', 'multipage'))
            mpc.job_result = result
            self.signal_job_message.emit('job ' + result.status + (': ' + result.error.splitlines()[0] if result.error else ''))
        except BaseException:
            import traceback
            traceback.print_exc()
            self.signal_job_message.emit('job runner crashed, see the console')
        self.processingHandler.processing_queue.join()
        self.processingHandler.upload_queue.join()
        elapsed_time = time.perf_counter_ns()-self.start_time
        print("Time taken for job: "+str(elapsed_time/10**9))
        self._emit_finished()

    def run(self):
        """Stability wrapper: whatever happens in the acquisition (camera read error, microcontroller timeout, ...)
        the illumination is switched off and 'finished' is emitted, so the GUI is released and the controller can
        clean up. Before, an exception left the GUI in the 'running' state with the light on, and Stop + Start then
        crashed the program (see tests/stress_acquisition.py)."""
        self._finished_emitted = False
        try:
            self._run_acquisition()
        except BaseException as e: # also SystemExit (microcontroller timeout calls sys.exit)
            import traceback
            traceback.print_exc()
            try:
                self._finish_merges() # single files of the finished positions are merged anyway
            except BaseException:
                traceback.print_exc()
            if isinstance(e, AcquisitionStopped):
                message = str(e)
            else:
                message = 'The acquisition stopped because of an error:\n' + type(e).__name__ + ': ' + str(e)
            print('*** ' + message.replace('\n',' '))
            try:
                self.liveController.turn_off_illumination()
            except BaseException:
                traceback.print_exc()
            try:
                self.navigationController.enable_joystick_button_action = True
            except BaseException:
                pass
            try:
                self.signal_acquisition_error.emit(message)
            except BaseException:
                pass
            self._flex_notify(dict(type='finished', status='failed', error=message))
            if not self._finished_emitted:
                self.finished.emit()

    def _emit_finished(self):
        self._finished_emitted = True
        self.finished.emit()

    def _run_acquisition(self):

        if getattr(self.multiPointController,'job_file',None):
            self.run_job_script()
            return

        self.start_time = time.perf_counter_ns()
        if self.camera.is_streaming == False:
             self.camera.start_streaming()

        if self.multiPointController.location_list is None:
            # use scanCoordinates for well plates or regular multipoint scan
            if self.multiPointController.scanCoordinates!=None and self.multiPointController.scanCoordinates.get_selected_wells():
                # use scan coordinates for the scan
                self.scan_coordinates_mm = self.multiPointController.scanCoordinates.coordinates_mm
                self.scan_coordinates_name = self.multiPointController.scanCoordinates.name
                self.use_scan_coordinates = True
            else:
                # use the current position for the scan
                self.scan_coordinates_mm = [(self.navigationController.x_pos_mm,self.navigationController.y_pos_mm)]
                self.scan_coordinates_name = ['']
                self.use_scan_coordinates = False
        else:
            # use location_list specified by the multipoint controlller
            self.scan_coordinates_mm = self.multiPointController.location_list
            self.scan_coordinates_name = None
            self.use_scan_coordinates = True

        # keep copy of original positions to measure drift
        self.scan_coordinates_mm_backup = self.scan_coordinates_mm.copy()

        self._open_position_log()
        self._merge_jobs = []
        self._merge_threads = []

        while self.time_point < self.Nt:
            # check if abort acquisition has been requested
            if self.multiPointController.abort_acqusition_requested:
                break
            # progress message for the live view: this time point starts now
            planned = self.time_point*self.dt if self.dt else None
            started = self._job_time()
            self._flex_notify(dict(type='timepoint', index=self.time_point, start=started, planned=planned, n=self.Nt,
                                   late=max(0.0,started-planned) if planned is not None else 0.0, interval=self.dt if self.dt else None,
                                   next_start=(self.time_point+1)*self.dt if (self.dt and self.time_point+1 < self.Nt) else None))
            # run single time point
            self.run_single_time_point()
            self._start_merge_thread() # single files of this time point -> multichannel files, in the background while the camera is idle
            self.time_point = self.time_point + 1
            # continous acquisition
            if self.dt == 0:
                pass
            # timed acquisition
            else:
                # check if the aquisition has taken longer than dt or integer multiples of dt, if so skip the next time point(s)
                while time.time() > self.timestamp_acquisition_started + self.time_point*self.dt:
                    print('skip time point ' + str(self.time_point+1))
                    self.time_point = self.time_point+1
                # check if it has reached Nt
                if self.time_point == self.Nt:
                    break # no waiting after taking the last time point
                # wait until it's time to do the next acquisition
                self._flex_notify(dict(type='state', state='waiting', tp=self.time_point-1, until=self.time_point*self.dt,
                                       reason='interval', next_tp=self.time_point))
                while time.time() < self.timestamp_acquisition_started + self.time_point*self.dt:
                    if self.multiPointController.abort_acqusition_requested:
                        break
                    time.sleep(0.05)
                self._flex_notify(dict(type='state', state='idle', tp=self.time_point))
        self._finish_merges()
        self.processingHandler.processing_queue.join()
        self.processingHandler.upload_queue.join()
        elapsed_time = time.perf_counter_ns()-self.start_time
        print("Time taken for acquisition/processing: "+str(elapsed_time/10**9))
        self._flex_notify(dict(type='finished', status='aborted' if self.multiPointController.abort_acqusition_requested else 'completed', error=''))
        self._emit_finished()

    def _start_merge_thread(self):
        """towbinlab: merge the single files of finished positions into their multichannel files (one image in memory at a time; the single
        files are deleted only after the merged file is verified). Runs in a thread, one at a time, while the camera waits for the next time point."""
        jobs, self._merge_jobs = getattr(self, '_merge_jobs', []), []
        if not jobs:
            return
        import threading
        self._merge_threads = [t for t in getattr(self, '_merge_threads', []) if t.is_alive()]
        def work(jobs=jobs, previous=list(self._merge_threads)):
            for t in previous: # one merge thread at a time (disk)
                t.join()
            from control.towbin_funs import merge_plane_tiffs
            for j in jobs:
                try:
                    ok, missing = merge_plane_tiffs(j['parts'], j['out'], j['channels'], j['numchannels'], j['num_z'])
                    if not ok:
                        print('*** merge: ' + os.path.basename(j['out']) + ' written, but images are missing (zeros) for (channel, z) ' + str(missing) + '; the single files were kept')
                except BaseException as e:
                    print('*** merge of ' + os.path.basename(j['out']) + ' failed (' + type(e).__name__ + ': ' + str(e) + '); the single files were kept')
        t = threading.Thread(target=work, name='merge-multichannel', daemon=False)
        t.start()
        self._merge_threads.append(t)

    def _finish_merges(self):
        self._start_merge_thread()
        for t in getattr(self, '_merge_threads', []):
            t.join()

    def wait_till_operation_is_completed(self):
        t0 = time.time()
        warned = False
        while self.microcontroller.is_busy():
            time.sleep(SLEEP_TIME_S)
            waited = time.time() - t0
            if waited > MCU_WAIT_WARN_S and not warned:
                warned = True
                print('*** the microcontroller has not finished the last command for ' + str(int(waited)) + ' s '
                      '(joystick or focus wheel touched during a move?)')
            if self.multiPointController.abort_acqusition_requested and waited > MCU_WAIT_ABORT_S:
                raise AcquisitionStopped('The acquisition was stopped while the stage had not finished its last move '
                                         '(the joystick or focus wheel may have been touched). Check the stage position.')
            if waited > MCU_WAIT_LIMIT_S:
                raise AcquisitionStopped('The stage did not finish a move within ' + str(int(MCU_WAIT_LIMIT_S)) + ' s '
                                         '(the joystick or focus wheel may have been touched during the acquisition). '
                                         'The acquisition was stopped; check the stage position.')

    def run_single_time_point(self):
        start = time.time()
        print(time.time())
        # disable joystick button action
        self.navigationController.enable_joystick_button_action = False

        print('multipoint acquisition - time point ' + str(self.time_point+1))
        
        # for each time point, create a new folder
        current_path = os.path.join(self.base_path,self.experiment_ID,str(self.time_point))
        os.mkdir(current_path)

        slide_path = os.path.join(self.base_path, self.experiment_ID)


        # create a dataframe to save coordinates
        if IS_HCS:
            if self.use_piezo:
                self.coordinates_pd = pd.DataFrame(columns = ['well', 'i', 'j', 'k', 'x (mm)', 'y (mm)', 'z (um)', 'z_piezo (um)', 'time'])
            else:
                self.coordinates_pd = pd.DataFrame(columns = ['well', 'i', 'j', 'k', 'x (mm)', 'y (mm)', 'z (um)', 'time'])
        else:
            if self.use_piezo:
                self.coordinates_pd = pd.DataFrame(columns = ['i', 'j', 'k', 'x (mm)', 'y (mm)', 'z (um)', 'z_piezo (um)', 'time'])
            else:
                self.coordinates_pd = pd.DataFrame(columns = ['i', 'j', 'k', 'x (mm)', 'y (mm)', 'z (um)', 'time'])


        n_regions = len(self.scan_coordinates_mm)

        for coordinate_id in range(n_regions):

            coordiante_mm = self.scan_coordinates_mm[coordinate_id]
            print(coordiante_mm)
            self._flex_state('moving', point=coordinate_id)

            if self.scan_coordinates_name is None:
                # flexible scan, use a sequencial ID
                coordiante_name = str(coordinate_id)
            else:
                coordiante_name = self.scan_coordinates_name[coordinate_id]
            
            if self.use_scan_coordinates:
                # calculate distance to last point
                if coordinate_id > 0:
                    next_coordiante_mm = self.scan_coordinates_mm[coordinate_id-1]
                    print(next_coordiante_mm)
                    distance_to_next_point = np.sqrt((coordiante_mm[0] - next_coordiante_mm[0])*(coordiante_mm[0] - next_coordiante_mm[0]) + (coordiante_mm[1]-next_coordiante_mm[1])*(coordiante_mm[1] - next_coordiante_mm[1]))
                    print('distance to next point: ' + str(distance_to_next_point))
                else:
                    distance_to_next_point = 14
                    print('distance to next point: ' + str(distance_to_next_point))
                                
                if (distance_to_next_point > 13):
                    # move z-position down
                    if coordiante_mm[2] >= self.navigationController.z_pos_mm:
                        self.navigationController.move_z_to(2)
                        self.wait_till_operation_is_completed()
                    else:
                        self.navigationController.move_z_to(2)
                        self.wait_till_operation_is_completed()
                        # remove backlash
                        _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                        self.navigationController.move_z_usteps(-_usteps_to_clear_backlash) # to-do: combine this with the above
                        self.wait_till_operation_is_completed()
                        self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                        self.wait_till_operation_is_completed()
                
                # move to the specified coordinate
                self.navigationController.move_x_to(coordiante_mm[0]-self.deltaX*(self.NX-1)/2)
                self.navigationController.move_y_to(coordiante_mm[1]-self.deltaY*(self.NY-1)/2)
                self.wait_till_operation_is_completed()
                if len(coordiante_mm) == 3:
                    if coordiante_mm[2] >= self.navigationController.z_pos_mm:
                        self.navigationController.move_z_to(coordiante_mm[2])
                        self.wait_till_operation_is_completed()
                    else:
                        self.navigationController.move_z_to(coordiante_mm[2])
                        self.wait_till_operation_is_completed()
                        # remove backlash
                        if self.navigationController.get_pid_control_flag(2) is False:
                            _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                            self.navigationController.move_z_usteps(-_usteps_to_clear_backlash) # to-do: combine this with the above
                            self.wait_till_operation_is_completed()
                            self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                            self.wait_till_operation_is_completed()
                else:
                    self.wait_till_operation_is_completed()
                time.sleep(SCAN_STABILIZATION_TIME_MS_Y/1000)
                if len(coordiante_mm) == 3:
                    time.sleep(SCAN_STABILIZATION_TIME_MS_Z/1000)
                # add '_' to the coordinate name
                coordiante_name = coordiante_name + '_'

            self._log_position('arrive',coordinate_id,note='at the position of the list (z of the list is the focus found at the previous time point, if autofocus is on)')

            self.x_scan_direction = 1
            self.dx_usteps = 0 # accumulated x displacement
            self.dy_usteps = 0 # accumulated y displacement
            self.dz_usteps = 0 # accumulated z displacement
            z_pos = self.navigationController.z_pos # zpos at the beginning of the scan

            # z stacking config
            if Z_STACKING_CONFIG == 'FROM TOP':
                self.deltaZ_usteps = -abs(self.deltaZ_usteps)

            if USE_NAPARI_FOR_MULTIPOINT or USE_NAPARI_FOR_TILED_DISPLAY:
                init_napari_layers = False

            # reset piezo to home position
            if self.use_piezo:
                self.z_piezo_um = OBJECTIVE_PIEZO_HOME_UM
                dac = int(65535 * (self.z_piezo_um / OBJECTIVE_PIEZO_RANGE_UM))
                self.navigationController.microcontroller.analog_write_onboard_DAC(7, dac)
                if self.liveController.trigger_mode == TriggerMode.SOFTWARE: # for hardware trigger, delay is in waiting for the last row to start exposure
                    time.sleep(MULTIPOINT_PIEZO_DELAY_MS/1000)
                if MULTIPOINT_PIEZO_UPDATE_DISPLAY:
                    self.signal_z_piezo_um.emit(self.z_piezo_um)

            # along y
            for i in range(self.NY):

                self.FOV_counter = 0 # for AF, so that AF at the beginning of each new row

                # along x
                for j in range(self.NX):

                    # for multi-channel image
                    self.image_multichannel = None
                    self.save_multichannel = False
                    self.merge_multichannel = False # towbinlab: write single files first, merge them into the multichannel file later
                    self._merge_parts = {}
                    if self.microscope.multiPointWidget2.checkbox_save_multichannel.isChecked():
                        self.save_multichannel = True
                        self.merge_multichannel = bool(getattr(self.microscope.multiPointWidget2, 'checkbox_merge_multichannel', None) and
                                                       self.microscope.multiPointWidget2.checkbox_merge_multichannel.isChecked())
                        numchannels = len(self.selected_configurations)
                        num_z = self.NZ


                    if RUN_CUSTOM_MULTIPOINT and "multipoint_custom_script_entry" in globals():

                        print('run custom multipoint')
                        multipoint_custom_script_entry(self,self.time_point,current_path,coordinate_id,coordiante_name,i,j)

                    else:

                        # autofocus
                        if self.do_reflection_af == False:
                            # contrast-based AF; perform AF only if when not taking z stack or doing z stack from center
                            if ( (self.NZ == 1) or Z_STACKING_CONFIG == 'FROM CENTER' ) and (self.do_autofocus) and (self.FOV_counter%Acquisition.NUMBER_OF_FOVS_PER_AF==0) and (self.autofocusController.use_focus_map or self.autofocusController.autofocus_scheduled(self.time_point,coordinate_id)):
                            # temporary: replace the above line with the line below to AF every FOV
                            # if (self.NZ == 1) and (self.do_autofocus):
                                configuration_name_AF = self.autofocusController.af_channel
                                config_AF = next((config for config in self.configurationManager.configurations if config.name == configuration_name_AF))
                                self._flex_state('autofocus', point=coordinate_id)
                                z_before_af_mm = self.navigationController.z_pos_mm
                                self.autofocusController.last_error = None
                                self.signal_current_configuration.emit(config_AF)
                                if (self.FOV_counter%Acquisition.NUMBER_OF_FOVS_PER_AF==0) or self.autofocusController.use_focus_map:
                                    self.autofocusController.autofocus()
                                    self.autofocusController.wait_till_autofocus_has_completed()
                                # upate z location of scan_coordinates_mm after AF but check if drift is not too large
                                threshold_z = 0.5 # in mm
                                af_note = ''
                                if len(coordiante_mm) == 3:
                                    z_dist_fraction = self.scan_coordinates_mm_backup[coordinate_id, 2] - self.navigationController.z_pos_mm
                                    if abs(z_dist_fraction) > threshold_z:
                                        print(f"Z position has changed by {z_dist_fraction} mm, which is larger than the threshold of {threshold_z} mm. "
                                          "This may indicate a problem with the stage or autofocus. No further z update will be performed.")
                                        af_note = f'focused z NOT stored: {z_dist_fraction*1000:.1f} um from the original z of this position (limit {threshold_z*1000:.0f} um)'
                                    else:
                                        self.scan_coordinates_mm[coordinate_id,2] = self.navigationController.z_pos_mm
                                        af_note = 'focused z stored in the positions list (used at the next time point)'
                                        # update the coordinate in the widget (signal: the table is changed in the GUI thread)
                                        self.signal_update_z.emit(coordinate_id,float(self.navigationController.z_pos_mm))
                                else:
                                    af_note = 'position has no z: nothing stored'
                                if getattr(self.autofocusController,'last_error',None):
                                    af_note = 'AUTOFOCUS FAILED (' + str(self.autofocusController.last_error) + '); ' + af_note
                                self._log_position('af',coordinate_id,note=af_note,z_before_af_mm=z_before_af_mm)
                            elif self.do_autofocus:
                                self._log_position('af_skipped',coordinate_id,note='autofocus not scheduled for this position at this time point (or not the first FOV)')
                        else:
                            # initialize laser autofocus if it has not been done
                            if self.microscope.laserAutofocusController.is_initialized==False:
                                # initialize the reflection AF
                                self.microscope.laserAutofocusController.initialize_auto()
                                # do contrast AF for the first FOV (if contrast AF box is checked)
                                if self.do_autofocus and ( (self.NZ == 1) or Z_STACKING_CONFIG == 'FROM CENTER' ) :
                                    configuration_name_AF = self.autofocusController.af_channel
                                    config_AF = next((config for config in self.configurationManager.configurations if config.name == configuration_name_AF))
                                    self.signal_current_configuration.emit(config_AF)
                                    self.autofocusController.autofocus()
                                    self.autofocusController.wait_till_autofocus_has_completed()
                                # set the current plane as reference
                                self.microscope.laserAutofocusController.set_reference()
                            else:
                                try:
                                    if self.navigationController.get_pid_control_flag(2) is False:
                                        self.microscope.laserAutofocusController.move_to_target(0)
                                        self.microscope.laserAutofocusController.move_to_target(0) # for stepper in open loop mode, repeat the operation to counter backlash
                                    else:
                                        self.microscope.laserAutofocusController.move_to_target(0)
                                except:
                                    file_ID = coordiante_name + str(i) + '_' + str(j if self.x_scan_direction==1 else self.NX-1-j)
                                    saving_path = os.path.join(current_path, file_ID + '_focus_camera.bmp')
                                    iio.imwrite(saving_path,self.microscope.laserAutofocusController.image) 
                                    print('!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! laser AF failed !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!')

                        if (self.NZ > 1):
                            # move to bottom of the z stack
                            if Z_STACKING_CONFIG == 'FROM CENTER':
                                self.navigationController.move_z_usteps(-self.deltaZ_usteps*round((self.NZ-1)/2))
                                self.wait_till_operation_is_completed()
                                time.sleep(SCAN_STABILIZATION_TIME_MS_Z/1000)
                            # maneuver for achiving uniform step size and repeatability when using open-loop control
                            self.navigationController.move_z_usteps(-160)
                            self.wait_till_operation_is_completed()
                            self.navigationController.move_z_usteps(160)
                            self.wait_till_operation_is_completed()
                            time.sleep(SCAN_STABILIZATION_TIME_MS_Z/1000)

                        # z-stack
                        for k in range(self.NZ):
                            
                            # Ensure that i/y-indexing is always top to bottom
                            sgn_i = -1 if self.deltaY >= 0 else 1
                            sgn_i = -sgn_i if INVERTED_OBJECTIVE else sgn_i
                            sgn_j = self.x_scan_direction if self.deltaX >= 0 else -self.x_scan_direction

                            real_i = self.NY-1-i if sgn_i == -1 else i
                            real_j = self.NX-1-j if sgn_j == -1 else j

                            file_ID = coordiante_name + str(self.NY-1-i if sgn_i == -1 else i) + '_' + str(j if sgn_j == 1 else self.NX-1-j) + '_' + str(k)
                            metadata = dict(x = self.navigationController.x_pos_mm, y = self.navigationController.y_pos_mm, z = self.navigationController.z_pos_mm)
                            print("scan coordinate:", metadata)
                            # metadata = json.dumps(metadata)

                            # laser af characterization mode
                            if LASER_AF_CHARACTERIZATION_MODE:
                                image = self.microscope.laserAutofocusController.get_image()
                                saving_path = os.path.join(current_path, file_ID + '_laser af camera' + '.bmp')
                                iio.imwrite(saving_path,image)

                            current_round_images = {}
                            # iterate through selected modes
                            self.channel_counter = 0
                            for config in self.selected_configurations:
                                self._flex_state('capturing', point=coordinate_id, channel=config.name)
                                if config.z_offset is not None: # perform z offset for config, assume
                                                                # z_offset is in um
                                    if config.z_offset != 0.0:
                                        print("Moving to Z offset "+str(config.z_offset))
                                        self.navigationController.move_z(config.z_offset/1000)
                                        self.wait_till_operation_is_completed()
                                        time.sleep(SCAN_STABILIZATION_TIME_MS_Z/1000)

                                if 'USB Spectrometer' not in config.name and 'RGB' not in config.name:
                                    # update the current configuration
                                    self.signal_current_configuration.emit(config)
                                    self.wait_till_operation_is_completed()
                                    # trigger acquisition (including turning on the illumination) and read frame
                                    if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                                        self.liveController.turn_on_illumination()
                                        self.wait_till_operation_is_completed()
                                        self.camera.send_trigger()
                                        image = self.camera.read_frame()
                                    elif self.liveController.trigger_mode == TriggerMode.HARDWARE:
                                        if 'Fluorescence' in config.name and ENABLE_NL5 and NL5_USE_DOUT:
                                            self.camera.image_is_ready = False # to remove
                                            self.microscope.nl5.start_acquisition()
                                            image = self.camera.read_frame(reset_image_ready_flag=False)
                                        else:
                                            self.microcontroller.send_hardware_trigger(control_illumination=True,illumination_on_time_us=self.camera.exposure_time*1000)
                                            image = self.camera.read_frame()
                                    
                                    if image is None:
                                        print('self.camera.read_frame() returned None')
                                        if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                                            self.liveController.turn_off_illumination() # stability: do not leave the light on
                                        continue
                                    # tunr of the illumination if using software trigger
                                    if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                                        self.liveController.turn_off_illumination()

                                    # process the image -  @@@ to move to camera
                                    image = utils.crop_image(image,self.crop_width,self.crop_height)
                                    image = utils.rotate_and_flip_image(image,rotate_image_angle=self.camera.rotate_image_angle,flip_image=self.camera.flip_image)
                                    # self.image_to_display.emit(cv2.resize(image,(round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling)),cv2.INTER_LINEAR))

                                    image_to_display = utils.crop_image(image,round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling))
                                    self.image_to_display.emit(image_to_display)
                                    self.image_to_display_multi.emit(image_to_display,config.illumination_source)

                                    if image.dtype == np.uint16:
                                        saving_path = os.path.join(current_path, file_ID + '_' + str(config.name).replace(' ','_') + '.tiff')
                                        if self.camera.is_color:
                                            if 'BF LED matrix' in config.name:
                                                if MULTIPOINT_BF_SAVING_OPTION == 'RGB2GRAY':
                                                    image = cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
                                                elif MULTIPOINT_BF_SAVING_OPTION == 'Green Channel Only':
                                                    image = image[:,:,1]
                                        #iio.imwrite(saving_path,image)
                                    else:
                                        saving_path = os.path.join(current_path, file_ID + '_' + str(config.name).replace(' ','_') + '.' + Acquisition.IMAGE_FORMAT)
                                        if self.camera.is_color:
                                            if 'BF LED matrix' in config.name:
                                                if MULTIPOINT_BF_SAVING_OPTION == 'RGB2GRAY':
                                                    image = cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
                                                elif MULTIPOINT_BF_SAVING_OPTION == 'Green Channel Only':
                                                    image = image[:,:,1]
                                        ##iio.imwrite(saving_path,image)
                                    
                                    from .towbin_funs import save_single_plane_tiff
                                    if not self.save_multichannel:
                                        save_single_plane_tiff(image, saving_path)
                                    elif self.merge_multichannel:
                                        # single file now (as without multichannel), merged into the multichannel file when the camera is idle
                                        save_single_plane_tiff(image, saving_path)
                                        self._merge_parts[(self.channel_counter, k)] = saving_path
                                        self.channel_counter += 1
                                    else:
                                        if self.image_multichannel is None:
                                            if image.dtype == np.uint16:
                                                self.image_multichannel = np.zeros((numchannels, num_z, image.shape[0], image.shape[1]), dtype=np.uint16)
                                            elif image.dtype == np.uint8:
                                                self.image_multichannel = np.zeros((numchannels, num_z, image.shape[0], image.shape[1]), dtype=np.uint8)
                                        self.image_multichannel[self.channel_counter, k, :, :] = image.copy()
                                        self.channel_counter += 1

                                    if USE_NAPARI_FOR_MULTIPOINT or USE_NAPARI_FOR_TILED_DISPLAY:
                                        if not init_napari_layers:
                                            print("init napari layers")
                                            init_napari_layers = True
                                            self.napari_layers_init.emit(image.shape[0],image.shape[1], image.dtype, False)
                                        self.napari_layers_update.emit(image, real_i, real_j, k, config.name)

                                    current_round_images[config.name] = np.copy(image)

                                    # dpc generation
                                    keys_to_check = ['BF LED matrix left half', 'BF LED matrix right half', 'BF LED matrix top half', 'BF LED matrix bottom half']
                                    if all(key in current_round_images for key in keys_to_check):
                                        # generate dpc
                                        pass

                                    # RGB generation
                                    keys_to_check = ['BF LED matrix full_R', 'BF LED matrix full_G', 'BF LED matrix full_B']
                                    if all(key in current_round_images for key in keys_to_check):
                                        print('constructing RGB image')
                                        print(current_round_images['BF LED matrix full_R'].dtype)
                                        size = current_round_images['BF LED matrix full_R'].shape
                                        rgb_image = np.zeros((*size, 3),dtype=current_round_images['BF LED matrix full_R'].dtype)
                                        print(rgb_image.shape)
                                        rgb_image[:, :, 0] = current_round_images['BF LED matrix full_R']
                                        rgb_image[:, :, 1] = current_round_images['BF LED matrix full_G']
                                        rgb_image[:, :, 2] = current_round_images['BF LED matrix full_B']

                                        # send image to display
                                        image_to_display = utils.crop_image(rgb_image,round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling))

                                        # write the image
                                        if len(rgb_image.shape) == 3:
                                            print('writing RGB image')
                                            if rgb_image.dtype == np.uint16:
                                                iio.imwrite(os.path.join(current_path, file_ID + '_BF_LED_matrix_full_RGB.tiff'), rgb_image)
                                            else:
                                                iio.imwrite(os.path.join(current_path, file_ID + '_BF_LED_matrix_full_RGB.' + Acquisition.IMAGE_FORMAT),rgb_image)

                                    QApplication.processEvents()

                                # RGB
                                elif 'RGB' in config.name:
                                    # go through the channels
                                    channels = ['BF LED matrix full_R', 'BF LED matrix full_G', 'BF LED matrix full_B']
                                    images = {}

                                    for config_ in self.configurationManager.configurations:
                                        if config_.name in channels:
                                            # update the current configuration
                                            self.signal_current_configuration.emit(config_)
                                            self.wait_till_operation_is_completed()

                                            # trigger acquisition (including turning on the illumination)
                                            if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                                                self.liveController.turn_on_illumination()
                                                self.wait_till_operation_is_completed()
                                                self.camera.send_trigger()
                                            elif self.liveController.trigger_mode == TriggerMode.HARDWARE:
                                                self.microcontroller.send_hardware_trigger(control_illumination=True, illumination_on_time_us=self.camera.exposure_time * 1000)

                                            # read camera frame
                                            image = self.camera.read_frame()
                                            if image is None:
                                                print('self.camera.read_frame() returned None')
                                                if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                                                    self.liveController.turn_off_illumination() # stability: do not leave the light on
                                                continue

                                            # turn off the illumination if using software trigger
                                            if self.liveController.trigger_mode == TriggerMode.SOFTWARE:
                                                self.liveController.turn_off_illumination()

                                            # process the image  -  @@@ to move to camera
                                            image = utils.crop_image(image, self.crop_width, self.crop_height)
                                            image = utils.rotate_and_flip_image(image, rotate_image_angle=self.camera.rotate_image_angle, flip_image=self.camera.flip_image)

                                            # add the image to dictionary
                                            images[config_.name] = np.copy(image)

                                    # Check if the image is RGB or monochrome
                                    i_size = images['BF LED matrix full_R'].shape
                                    i_dtype = images['BF LED matrix full_R'].dtype

                                    if len(i_size) == 3:
                                        # If already RGB, write and emit individual channels
                                        print('writing R, G, B channels')

                                        for channel in channels:
                                            image_to_display = utils.crop_image(images[channel], round(self.crop_width * self.display_resolution_scaling), round(self.crop_height * self.display_resolution_scaling))
                                            self.image_to_display.emit(image_to_display)
                                            self.image_to_display_multi.emit(image_to_display, config.illumination_source)

                                            if USE_NAPARI_FOR_MULTIPOINT or USE_NAPARI_FOR_TILED_DISPLAY:
                                                if not init_napari_layers:
                                                    print(f"init napari {channel} layer")
                                                    init_napari_layers = True
                                                    self.napari_layers_init.emit(i_size[0], i_size[1], i_dtype, True)
                                                self.napari_layers_update.emit(images[channel], real_i, real_j, k, channel)

                                            file_name = file_ID + '_' + channel.replace(' ', '_') + ('.tiff' if i_dtype == np.uint16 else '.' + Acquisition.IMAGE_FORMAT)
                                            iio.imwrite(os.path.join(current_path, file_name), images[channel])

                                    else:
                                        # If monochrome, reconstruct RGB image
                                        print('constructing RGB image')

                                        rgb_image = np.zeros((*i_size, 3), dtype=i_dtype)
                                        rgb_image[:, :, 0] = images['BF LED matrix full_R']
                                        rgb_image[:, :, 1] = images['BF LED matrix full_G']
                                        rgb_image[:, :, 2] = images['BF LED matrix full_B']

                                        # send image to display
                                        image_to_display = utils.crop_image(rgb_image, round(self.crop_width * self.display_resolution_scaling), round(self.crop_height * self.display_resolution_scaling))
                                        self.image_to_display.emit(image_to_display)
                                        self.image_to_display_multi.emit(image_to_display, config.illumination_source)

                                        if USE_NAPARI_FOR_MULTIPOINT or USE_NAPARI_FOR_TILED_DISPLAY:
                                            if not init_napari_layers:
                                                print("init napari rgb layer")
                                                init_napari_layers = True
                                                print(rgb_image.dtype)
                                                self.napari_layers_init.emit(rgb_image.shape[0], rgb_image.shape[1], rgb_image.dtype, True)
                                            self.napari_layers_update.emit(rgb_image, real_i, real_j, k, config.name)

                                        # write the RGB image
                                        print('writing RGB image')
                                        file_name = file_ID + '_BF_LED_matrix_full_RGB' + ('.tiff' if rgb_image.dtype == np.uint16 else '.' + Acquisition.IMAGE_FORMAT)
                                        iio.imwrite(os.path.join(current_path, file_name), rgb_image)

                                # USB spectrometer
                                else:
                                    if self.usb_spectrometer != None:
                                        for l in range(N_SPECTRUM_PER_POINT):
                                            data = self.usb_spectrometer.read_spectrum()
                                            self.spectrum_to_display.emit(data)
                                            saving_path = os.path.join(current_path, file_ID + '_' + str(config.name).replace(' ','_') + '_' + str(l) + '.csv')
                                            np.savetxt(saving_path,data,delimiter=',')
                                
                                self._flex_done('image', coordinate_id, config.name)
                                if config.z_offset is not None: # undo Z offset
                                                                # assume z_offset is in um
                                    if config.z_offset != 0.0:
                                        print("Moving back from Z offset "+str(config.z_offset))
                                        self.navigationController.move_z(-config.z_offset/1000)
                                        self.wait_till_operation_is_completed()
                                        time.sleep(SCAN_STABILIZATION_TIME_MS_Z/1000)

                            # tiled preview
                            if SHOW_TILED_PREVIEW and 'BF LED matrix full' in current_round_images:
                                # initialize the variable
                                if self.tiled_preview is None:
                                    size = current_round_images['BF LED matrix full'].shape
                                    if len(size) == 2:
                                        self.tiled_preview = np.zeros((int(self.NY*size[0]/PRVIEW_DOWNSAMPLE_FACTOR),self.NX*int(size[1]/PRVIEW_DOWNSAMPLE_FACTOR)),dtype=current_round_images['BF LED matrix full'].dtype)
                                    else:
                                        self.tiled_preview = np.zeros((int(self.NY*size[0]/PRVIEW_DOWNSAMPLE_FACTOR),self.NX*int(size[1]/PRVIEW_DOWNSAMPLE_FACTOR),size[2]),dtype=current_round_images['BF LED matrix full'].dtype)
                                # downsample the image
                                I = current_round_images['BF LED matrix full']
                                width = int(I.shape[1]/PRVIEW_DOWNSAMPLE_FACTOR)
                                height = int(I.shape[0]/PRVIEW_DOWNSAMPLE_FACTOR)
                                I = cv2.resize(I, (width,height), interpolation=cv2.INTER_AREA)
                                # populate the tiled_preview
                                self.tiled_preview[(self.NY-real_i-1)*height:(self.NY-real_i)*height, real_j*width:(real_j+1)*width, ] = I
                                # emit the result
                                self.image_to_display_tiled_preview.emit(self.tiled_preview)

                            # add the coordinate of the current location
                            if IS_HCS:
                                if self.use_piezo:
                                    new_row = pd.DataFrame({'well': coordiante_name.replace("_", ""),
                                                            'i':[self.NY-1-i if sgn_i == -1 else i],'j':[j if sgn_j == 1 else self.NX-1-j],'k':[k],
                                                            'x (mm)':[self.navigationController.x_pos_mm],
                                                            'y (mm)':[self.navigationController.y_pos_mm],
                                                            'z (um)':[self.navigationController.z_pos_mm*1000],
                                                            'z_piezo (um)':[self.z_piezo_um-OBJECTIVE_PIEZO_HOME_UM],
                                                            'time':datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')})
                                else:
                                    new_row = pd.DataFrame({'well': coordiante_name.replace("_", ""),
                                                            'i':[self.NY-1-i if sgn_i == -1 else i],'j':[j if sgn_j == 1 else self.NX-1-j],'k':[k],
                                                            'x (mm)':[self.navigationController.x_pos_mm],
                                                            'y (mm)':[self.navigationController.y_pos_mm],
                                                            'z (um)':[self.navigationController.z_pos_mm*1000],
                                                            'time':datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')})
                            else:
                                if self.use_piezo:
                                    new_row = pd.DataFrame({'i':[self.NY-1-i if sgn_i == -1 else i],'j':[j if sgn_j == 1 else self.NX-1-j],'k':[k],
                                                            'x (mm)':[self.navigationController.x_pos_mm],
                                                            'y (mm)':[self.navigationController.y_pos_mm],
                                                            'z (um)':[self.navigationController.z_pos_mm*1000],
                                                            'z_piezo (um)':[self.z_piezo_um-OBJECTIVE_PIEZO_HOME_UM],
                                                            'time':datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')})
                                else:
                                    new_row = pd.DataFrame({'i':[self.NY-1-i if sgn_i == -1 else i],'j':[j if sgn_j == 1 else self.NX-1-j],'k':[k],
                                                            'x (mm)':[self.navigationController.x_pos_mm],
                                                            'y (mm)':[self.navigationController.y_pos_mm],
                                                            'z (um)':[self.navigationController.z_pos_mm*1000],
                                                            'time':datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')})

                            self.coordinates_pd = pd.concat([self.coordinates_pd, new_row], ignore_index=True)

                            # register the current fov in the navigationViewer
                            self.signal_register_current_fov.emit(self.navigationController.x_pos_mm,self.navigationController.y_pos_mm)

                            # check if the acquisition should be aborted
                            if self.multiPointController.abort_acqusition_requested:
                                self.liveController.turn_off_illumination()
                                self.navigationController.move_x_usteps(-self.dx_usteps)
                                self.wait_till_operation_is_completed()
                                self.navigationController.move_y_usteps(-self.dy_usteps)
                                self.wait_till_operation_is_completed()

                                if self.navigationController.get_pid_control_flag(2) is False:
                                    _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                                    self.navigationController.move_z_usteps(-self.dz_usteps-_usteps_to_clear_backlash)
                                    self.wait_till_operation_is_completed()
                                    self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                                    self.wait_till_operation_is_completed()
                                else:
                                    self.navigationController.move_z_usteps(-self.dz_usteps)
                                    self.wait_till_operation_is_completed()

                                self.coordinates_pd.to_csv(os.path.join(current_path,'coordinates.csv'),index=False,header=True)
                                self.navigationController.enable_joystick_button_action = True
                                return

                            if self.NZ > 1:
                                # move z
                                if k < self.NZ - 1:
                                    if self.use_piezo:
                                        self.z_piezo_um += self.deltaZ*1000
                                        dac = int(65535 * (self.z_piezo_um / OBJECTIVE_PIEZO_RANGE_UM))
                                        self.navigationController.microcontroller.analog_write_onboard_DAC(7, dac)
                                        if self.liveController.trigger_mode == TriggerMode.SOFTWARE: # for hardware trigger, delay is in waiting for the last row to start exposure
                                            time.sleep(MULTIPOINT_PIEZO_DELAY_MS/1000)
                                        if MULTIPOINT_PIEZO_UPDATE_DISPLAY:
                                            self.signal_z_piezo_um.emit(self.z_piezo_um)
                                    else:
                                        self.navigationController.move_z_usteps(self.deltaZ_usteps)
                                        self.wait_till_operation_is_completed()
                                        time.sleep(SCAN_STABILIZATION_TIME_MS_Z/1000)
                                        self.dz_usteps = self.dz_usteps + self.deltaZ_usteps

                        # z and channel finished. Save multichannel image if needed
                        if self.save_multichannel and self.merge_multichannel and self._merge_parts:
                            multi_channel_id = f"Time{self.time_point :04d}_Point{int(coordinate_id) :04d}"
                            self._merge_jobs.append(dict(parts=dict(self._merge_parts), out=os.path.join(self.base_path, self.experiment_ID, multi_channel_id + ".tiff"),
                                                         channels=[config.name for config in self.selected_configurations],
                                                         numchannels=numchannels, num_z=num_z))
                            self._merge_parts = {}
                        elif self.save_multichannel and self.image_multichannel is not None:
                            multi_channel_id = f"Time{self.time_point :04d}_Point{int(coordinate_id) :04d}"
                            channel_names = [config.name for config in self.selected_configurations]
                            saving_path = os.path.join(self.base_path, self.experiment_ID, multi_channel_id + ".tiff")
                            save_single_plane_tiff(self.image_multichannel, saving_path, channels=channel_names)

                        
                        if self.NZ > 1:
                            # move z back
                            if self.use_piezo:
                                self.z_piezo_um = OBJECTIVE_PIEZO_HOME_UM
                                dac = int(65535 * (self.z_piezo_um / OBJECTIVE_PIEZO_RANGE_UM))
                                self.navigationController.microcontroller.analog_write_onboard_DAC(7, dac)
                                if self.liveController.trigger_mode == TriggerMode.SOFTWARE: # for hardware trigger, delay is in waiting for the last row to start exposure
                                    time.sleep(MULTIPOINT_PIEZO_DELAY_MS/1000)
                                if MULTIPOINT_PIEZO_UPDATE_DISPLAY:
                                    self.signal_z_piezo_um.emit(self.z_piezo_um)
                            else:
                                _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                                if Z_STACKING_CONFIG == 'FROM CENTER':
                                    if self.navigationController.get_pid_control_flag(2) is False:
                                        _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                                        self.navigationController.move_z_usteps( -self.deltaZ_usteps*(self.NZ-1) + self.deltaZ_usteps*round((self.NZ-1)/2) - _usteps_to_clear_backlash)
                                        self.wait_till_operation_is_completed()
                                        self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                                        self.wait_till_operation_is_completed()
                                    else:
                                        self.navigationController.move_z_usteps( -self.deltaZ_usteps*(self.NZ-1) + self.deltaZ_usteps*round((self.NZ-1)/2) )
                                        self.wait_till_operation_is_completed()
                                    self.dz_usteps = self.dz_usteps - self.deltaZ_usteps*(self.NZ-1) + self.deltaZ_usteps*round((self.NZ-1)/2)
                                else:
                                    if self.navigationController.get_pid_control_flag(2) is False:
                                        _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                                        self.navigationController.move_z_usteps(-self.deltaZ_usteps*(self.NZ-1) - _usteps_to_clear_backlash)
                                        self.wait_till_operation_is_completed()
                                        self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                                        self.wait_till_operation_is_completed()
                                    else:
                                        self.navigationController.move_z_usteps(-self.deltaZ_usteps*(self.NZ-1))
                                        self.wait_till_operation_is_completed()
                                    self.dz_usteps = self.dz_usteps - self.deltaZ_usteps*(self.NZ-1)

                        # update FOV counter
                        self.FOV_counter = self.FOV_counter + 1

                    if self.NX > 1:
                        # move x
                        if j < self.NX - 1:
                            self.navigationController.move_x_usteps(self.x_scan_direction*self.deltaX_usteps)
                            self.wait_till_operation_is_completed()
                            time.sleep(SCAN_STABILIZATION_TIME_MS_X/1000)
                            self.dx_usteps = self.dx_usteps + self.x_scan_direction*self.deltaX_usteps

                # finished X scan
                '''
                # instead of move back, reverse scan direction (12/29/2021)
                if self.NX > 1:
                    # move x back
                    self.navigationController.move_x_usteps(-self.deltaX_usteps*(self.NX-1))
                    self.wait_till_operation_is_completed()
                    time.sleep(SCAN_STABILIZATION_TIME_MS_X/1000)
                '''
                self.x_scan_direction = -self.x_scan_direction

                if self.NY > 1:
                    # move y
                    if i < self.NY - 1:
                        self.navigationController.move_y_usteps(self.deltaY_usteps)
                        self.wait_till_operation_is_completed()
                        time.sleep(SCAN_STABILIZATION_TIME_MS_Y/1000)
                        self.dy_usteps = self.dy_usteps + self.deltaY_usteps

            # finished XY scan
            if SHOW_TILED_PREVIEW and IS_HCS:
                self.navigationController.keep_scan_begin_position(self.navigationController.x_pos_mm, self.navigationController.y_pos_mm)

            if n_regions == 1:
                # only move to the start position if there's only one region in the scan
                if self.NY > 1:
                    # move y back
                    self.navigationController.move_y_usteps(-self.deltaY_usteps*(self.NY-1))
                    self.wait_till_operation_is_completed()
                    time.sleep(SCAN_STABILIZATION_TIME_MS_Y/1000)
                    self.dy_usteps = self.dy_usteps - self.deltaY_usteps*(self.NY-1)

                if SHOW_TILED_PREVIEW and not IS_HCS:
                    self.navigationController.keep_scan_begin_position(self.navigationController.x_pos_mm, self.navigationController.y_pos_mm)

                # move x back at the end of the scan
                if self.x_scan_direction == -1:
                    self.navigationController.move_x_usteps(-self.deltaX_usteps*(self.NX-1))
                    self.wait_till_operation_is_completed()
                    time.sleep(SCAN_STABILIZATION_TIME_MS_X/1000)

                # move z back
                if self.navigationController.get_pid_control_flag(2) is False:
                    _usteps_to_clear_backlash = max(160,20*self.navigationController.z_microstepping)
                    self.navigationController.microcontroller.move_z_to_usteps(z_pos - STAGE_MOVEMENT_SIGN_Z*_usteps_to_clear_backlash)
                    self.wait_till_operation_is_completed()
                    self.navigationController.move_z_usteps(_usteps_to_clear_backlash)
                    self.wait_till_operation_is_completed()
                else:
                    self.navigationController.microcontroller.move_z_to_usteps(z_pos)
                    self.wait_till_operation_is_completed()

            self._log_position('end',coordinate_id,note='position finished')
            self._flex_done('position', coordinate_id)

        # finished region scan
        self.coordinates_pd.to_csv(os.path.join(current_path,'coordinates.csv'),index=False,header=True)
        self.navigationController.enable_joystick_button_action = True
        print(time.time())
        print(time.time()-start)

class MultiPointController(QObject):

    acquisitionFinished = Signal()
    image_to_display = Signal(np.ndarray)
    image_to_display_multi = Signal(np.ndarray,int)
    image_to_display_tiled_preview = Signal(np.ndarray)
    spectrum_to_display = Signal(np.ndarray)
    signal_current_configuration = Signal(Configuration)
    signal_register_current_fov = Signal(float,float)
    detection_stats = Signal(object)
    napari_layers_update = Signal(np.ndarray, int, int, int, str)
    napari_layers_init = Signal(int, int, object, bool)
    signal_z_piezo_um = Signal(float)
    signal_job_message = Signal(str)   # JOBs: progress messages of a job script
    signal_job_status = Signal(object) # JOBs: live status (dicts, see control/jobs_live.py)
    signal_acquisition_error = Signal(str) # stability: an acquisition ended because of an error (message for the user)
    acquisitionStarted = Signal()      # stability: emitted when an acquisition thread is started (input lock)
    signal_update_z = Signal(int,float) # towbinlab: (position index, z in mm): the focused z of a position (Positions table)
    signal_flex_status = Signal(object) # towbinlab: progress of a Flexible Multipoint acquisition (live view, see control/jobs_live.py)

    def __init__(self,camera,navigationController,liveController,autofocusController,configurationManager,usb_spectrometer=None,scanCoordinates=None,parent=None):
        QObject.__init__(self)

        self.camera = camera
        self.processingHandler = ProcessingHandler()
        self.microcontroller = navigationController.microcontroller # to move to gui for transparency
        self.navigationController = navigationController
        self.liveController = liveController
        self.autofocusController = autofocusController
        self.configurationManager = configurationManager
        self.NX = 1
        self.NY = 1
        self.NZ = 1
        self.Nt = 1
        mm_per_ustep_X = SCREW_PITCH_X_MM/(self.navigationController.x_microstepping*FULLSTEPS_PER_REV_X)
        mm_per_ustep_Y = SCREW_PITCH_Y_MM/(self.navigationController.y_microstepping*FULLSTEPS_PER_REV_Y)
        mm_per_ustep_Z = SCREW_PITCH_Z_MM/(self.navigationController.z_microstepping*FULLSTEPS_PER_REV_Z)
        self.deltaX = Acquisition.DX
        self.deltaX_usteps = round(self.deltaX/mm_per_ustep_X)
        self.deltaY = Acquisition.DY
        self.deltaY_usteps = round(self.deltaY/mm_per_ustep_Y)
        self.deltaZ = Acquisition.DZ/1000
        self.deltaZ_usteps = round(self.deltaZ/mm_per_ustep_Z)
        self.deltat = 0
        self.do_autofocus = False
        self.do_reflection_af = False
        self.gen_focus_map = False
        self.focus_map_storage = []
        self.already_using_fmap = False
        self.crop_width = Acquisition.CROP_WIDTH
        self.crop_height = Acquisition.CROP_HEIGHT
        self.display_resolution_scaling = Acquisition.IMAGE_DISPLAY_SCALING_FACTOR
        self.counter = 0
        self.experiment_ID = None
        self.base_path = None
        self.use_piezo = MULTIPOINT_USE_PIEZO_FOR_ZSTACKS #TODO: change to false and get value from widget
        self.selected_configurations = []
        self.usb_spectrometer = usb_spectrometer
        self.scanCoordinates = scanCoordinates
        self.parent = parent
        self.old_images_per_page = 1
        try:
            if self.parent is not None:
                self.old_images_per_page = self.parent.dataHandler.n_images_per_page
        except:
            pass
        self.location_list = None # for flexible multipoint
        self.job_file = None # JOBs: path of the job file to run instead of the standard loops (None = standard acquisition)
        self.job_points = None # JOBs: list of control.jobs.Point
        self.job_save_as = 'multipage' # JOBs: 'multipage' (Time0003_Point0002.tiff, written directly), 'merge' (single files merged later) or 'single' (single files per image)
        self.job_result = None
        self.job_pause_requested = False # JOBs: True while the running job should be paused (set from the GUI)
        self.job_new_positions = None # JOBs: positions (x,y,z list) read from the table at resume
        self._old_threads = [] # stability: keep finished-but-not-yet-stopped acquisition threads alive until they end
        self.last_acquisition_error = None

    def set_NX(self,N):
        self.NX = N
    def set_NY(self,N):
        self.NY = N
    def set_NZ(self,N):
        self.NZ = N
    def set_Nt(self,N):
        self.Nt = N
    def set_deltaX(self,delta):
        mm_per_ustep_X = SCREW_PITCH_X_MM/(self.navigationController.x_microstepping*FULLSTEPS_PER_REV_X)
        self.deltaX = delta
        self.deltaX_usteps = round(delta/mm_per_ustep_X)
    def set_deltaY(self,delta):
        mm_per_ustep_Y = SCREW_PITCH_Y_MM/(self.navigationController.y_microstepping*FULLSTEPS_PER_REV_Y)
        self.deltaY = delta
        self.deltaY_usteps = round(delta/mm_per_ustep_Y)
    def set_deltaZ(self,delta_um):
        mm_per_ustep_Z = SCREW_PITCH_Z_MM/(self.navigationController.z_microstepping*FULLSTEPS_PER_REV_Z)
        self.deltaZ = delta_um/1000
        self.deltaZ_usteps = round((delta_um/1000)/mm_per_ustep_Z)
    def set_deltat(self,delta):
        self.deltat = delta
    def set_af_flag(self,flag):
        self.do_autofocus = flag
    def set_reflection_af_flag(self,flag):
        self.do_reflection_af = flag
    def set_gen_focus_map_flag(self, flag):
        self.gen_focus_map = flag
        if not flag:
            self.autofocusController.set_focus_map_use(False)
    def set_crop(self,crop_width, crop_height):
        self.crop_width = crop_width
        self.crop_height = crop_height

    def set_base_path(self,path):
        self.base_path = path

    def start_new_experiment(self,experiment_ID): # @@@ to do: change name to prepare_folder_for_new_experiment
        # generate unique experiment ID
        self.experiment_ID = experiment_ID.replace(' ','_') + '_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')
        self.recording_start_time = time.time()
        # create a new folder
        os.mkdir(os.path.join(self.base_path,self.experiment_ID))
        configManagerThrowaway = ConfigurationManager(self.configurationManager.config_filename)
        configManagerThrowaway.write_configuration_selected(self.selected_configurations,os.path.join(self.base_path,self.experiment_ID)+"/configurations.xml") # save the configuration for the experiment
        acquisition_parameters = {'dx(mm)':self.deltaX, 'Nx':self.NX, 'dy(mm)':self.deltaY, 'Ny':self.NY, 'dz(um)':self.deltaZ*1000,'Nz':self.NZ,'dt(s)':self.deltat,'Nt':self.Nt,'with AF':self.do_autofocus,'with reflection AF':self.do_reflection_af}
        try: # write objective data if it is available
            current_objective = self.parent.objectiveStore.current_objective
            objective_info = self.parent.objectiveStore.objectives_dict.get(current_objective, {})
            acquisition_parameters['objective'] = {}
            for k in objective_info.keys():
                acquisition_parameters['objective'][k]=objective_info[k]
            acquisition_parameters['objective']['name']=current_objective
        except:
            try:
                objective_info = OBJECTIVES[DEFAULT_OBJECTIVE]
                acquisition_parameters['objective'] = {}
                for k in objective_info.keys():
                    acquisition_parameters['objective'][k] = objective_info[k]
                acquisition_parameters['objective']['name']=DEFAULT_OBJECTIVE
            except:
                pass
        acquisition_parameters['sensor_pixel_size_um'] = CAMERA_PIXEL_SIZE_UM[CAMERA_SENSOR]
        acquisition_parameters['tube_lens_mm'] = TUBE_LENS_MM
        f = open(os.path.join(self.base_path,self.experiment_ID)+"/acquisition parameters.json","w")
        f.write(json.dumps(acquisition_parameters))
        f.close()


    def set_selected_configurations(self, selected_configurations_name):
        self.selected_configurations = []
        for configuration_name in selected_configurations_name:
            self.selected_configurations.append(next((config for config in self.configurationManager.configurations if config.name == configuration_name)))
        
    def run_job(self, job_file, points, save_as='multipage'):
        """JOBs: run a job file (control/jobs) with the given list of control.jobs.Point. Same preparation as a
        standard acquisition (call set_base_path, set_selected_configurations and start_new_experiment first)."""
        self.job_file = job_file
        self.job_points = points
        self.job_save_as = save_as
        self.job_result = None
        self.run_acquisition(np.array([[p.x,p.y,p.z] for p in points],dtype=float),_job=True)

    def acquisition_running(self):
        """stability: True while the acquisition thread of the previous run has not ended yet."""
        if getattr(self, '_acquisition_done', True):
            return False # the worker has finished; its thread may still be shutting down (it is kept in _old_threads)
        t = self.__dict__.get('thread')
        try:
            return isinstance(t, QThread) and t.isRunning()
        except RuntimeError: # already deleted
            return False

    def run_acquisition(self, location_list=None, _job=False): # @@@ to do: change name to run_experiment
        if self.acquisition_running():
            # stability: starting a second acquisition while the first one is still ending would run two workers on
            # the same hardware and destroy a running QThread ("QThread: Destroyed while thread is still running")
            print('*** an acquisition is still running (or still stopping); the new one was not started')
            return False
        self.last_acquisition_error = None
        if not _job:
            self.job_file = None # a standard acquisition never runs a leftover job
        # optional check before the start (set by CameraROIWidget): the hardware ROI must be centred and equal to the software crops
        # BEFORE the worker copies the crops below
        hook = getattr(self, 'before_start_hook', None)
        if hook is not None:
            try:
                hook()
            except Exception as e:
                print('*** camera ROI check before the start failed: ' + str(e))
        print('start multipoint')
        print(str(self.Nt) + '_' + str(self.NX) + '_' + str(self.NY) + '_' + str(self.NZ))

        if location_list is not None:
            print(location_list)
            self.location_list = location_list
        else:
            self.location_list = None

        self.abort_acqusition_requested = False
        self.job_pause_requested = False
        self.job_new_positions = None

        self.configuration_before_running_multipoint = self.liveController.currentConfiguration
        # stop live
        if self.liveController.is_live:
            self.liveController_was_live_before_multipoint = True
            self.liveController.stop_live() # @@@ to do: also uncheck the live button
        else:
            self.liveController_was_live_before_multipoint = False

        # disable callback
        if self.camera.callback_is_enabled:
            self.camera_callback_was_enabled_before_multipoint = True
            self.camera.disable_callback()
        else:
            self.camera_callback_was_enabled_before_multipoint = False

        if self.usb_spectrometer != None:
            if self.usb_spectrometer.streaming_started == True and self.usb_spectrometer.streaming_paused == False:
                self.usb_spectrometer.pause_streaming()
                self.usb_spectrometer_was_streaming = True
            else:
                self.usb_spectrometer_was_streaming = False

        if self.parent is not None:
            try:
                self.parent.imageDisplayTabs.setCurrentWidget(self.parent.imageArrayDisplayWindow.widget)
            except:
                pass
            try:
                self.parent.recordTabWidget.setCurrentWidget(self.parent.statsDisplayWidget)
            except:
                pass
        
        # run the acquisition
        self.timestamp_acquisition_started = time.time()

        if SHOW_TILED_PREVIEW:
            self.navigationController.keep_scan_begin_position(self.navigationController.x_pos_mm, self.navigationController.y_pos_mm)

        # create a QThread object
        if self.gen_focus_map and not self.do_reflection_af:
            print("Generating focus map for multipoint grid")
            starting_x_mm = self.navigationController.x_pos_mm
            starting_y_mm = self.navigationController.y_pos_mm
            fmap_Nx = max(2,self.NX-1)
            fmap_Ny = max(2,self.NY-1)
            fmap_dx = self.deltaX
            fmap_dy = self.deltaY
            if abs(fmap_dx) < 0.1 and fmap_dx != 0.0:
                fmap_dx = 0.1*fmap_dx/(abs(fmap_dx))
            elif fmap_dx == 0.0:
                fmap_dx = 0.1
            if abs(fmap_dy) < 0.1 and fmap_dy != 0.0:
                 fmap_dy = 0.1*fmap_dy/(abs(fmap_dy))
            elif fmap_dy == 0.0:
                fmap_dy = 0.1
            try:
                self.focus_map_storage = []
                self.already_using_fmap = self.autofocusController.use_focus_map
                for x,y,z in self.autofocusController.focus_map_coords:
                    self.focus_map_storage.append((x,y,z))
                coord1 = (starting_x_mm, starting_y_mm)
                coord2 = (starting_x_mm+fmap_Nx*fmap_dx,starting_y_mm)
                coord3 = (starting_x_mm,starting_y_mm+fmap_Ny*fmap_dy)
                self.autofocusController.gen_focus_map(coord1, coord2, coord3)
                self.autofocusController.set_focus_map_use(True)
                self.navigationController.move_to(starting_x_mm, starting_y_mm)
                self.navigationController.microcontroller.wait_till_operation_is_completed()
            except ValueError:
                print("Invalid coordinates for focus map, aborting.")
                return

        old = self.__dict__.get('thread')
        if isinstance(old, QThread):
            self._old_threads.append(old) # stability: a QThread must not be destroyed before it has really stopped
        self._old_threads = [t for t in self._old_threads if not t.isFinished()]
        self._set_joystick_enabled(False) # stability: joystick / focus wheel off during the acquisition (needs the towbinlab firmware)
        self.thread = QThread()
        self._acquisition_done = False
        # create a worker object
        self.processingHandler.start_processing()
        self.processingHandler.start_uploading()
        self.multiPointWorker = MultiPointWorker(self)
        # move the worker to the thread
        self.multiPointWorker.moveToThread(self.thread)
        # connect signals and slots
        self.thread.started.connect(self.multiPointWorker.run)
        self.multiPointWorker.signal_detection_stats.connect(self.slot_detection_stats)
        self.multiPointWorker.finished.connect(self._on_acquisition_completed)
        self.multiPointWorker.finished.connect(self.multiPointWorker.deleteLater)
        self.multiPointWorker.finished.connect(self.thread.quit)
        self.multiPointWorker.image_to_display.connect(self.slot_image_to_display)
        self.multiPointWorker.image_to_display_multi.connect(self.slot_image_to_display_multi)
        self.multiPointWorker.image_to_display_tiled_preview.connect(self.slot_image_to_display_tiled_preview)
        self.multiPointWorker.spectrum_to_display.connect(self.slot_spectrum_to_display)
        self.multiPointWorker.signal_current_configuration.connect(self.slot_current_configuration,type=Qt.BlockingQueuedConnection)
        self.multiPointWorker.signal_job_message.connect(self.signal_job_message)
        self.multiPointWorker.signal_job_status.connect(self.signal_job_status)
        self.multiPointWorker.signal_update_z.connect(self.signal_update_z)
        self.multiPointWorker.signal_flex_status.connect(self.signal_flex_status)
        self.multiPointWorker.signal_acquisition_error.connect(self._on_acquisition_error)
        self.multiPointWorker.signal_register_current_fov.connect(self.slot_register_current_fov)
        self.multiPointWorker.napari_layers_init.connect(self.slot_napari_layers_init)
        self.multiPointWorker.napari_layers_update.connect(self.slot_napari_layers_update)
        self.multiPointWorker.signal_z_piezo_um.connect(self.slot_z_piezo_um)
        # self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.thread.quit)
        # start the thread
        self.thread.start()
        self.acquisitionStarted.emit()
        return True

    def _set_joystick_enabled(self, enabled):
        if not LOCK_JOYSTICK_DURING_ACQUISITION:
            return
        try:
            self.microcontroller.set_joystick_enabled(enabled)
        except Exception as e:
            print('joystick lock not available: ' + str(e))

    def _on_acquisition_error(self, message):
        self.last_acquisition_error = message
        self.signal_acquisition_error.emit(message)

    def _on_acquisition_completed(self):
        self._acquisition_done = True
        self._set_joystick_enabled(True)
        # restore the previous selected mode
        if self.gen_focus_map:
            self.autofocusController.clear_focus_map()
            for x,y,z in self.focus_map_storage:
                self.autofocusController.focus_map_coords.append((x,y,z))
            self.autofocusController.use_focus_map = self.already_using_fmap
        self.signal_current_configuration.emit(self.configuration_before_running_multipoint)

        # re-enable callback
        if self.camera_callback_was_enabled_before_multipoint:
            self.camera.enable_callback()
            self.camera_callback_was_enabled_before_multipoint = False
        
        # re-enable live if it's previously on
        if self.liveController_was_live_before_multipoint:
            self.liveController.start_live()

        if self.usb_spectrometer != None:
            if self.usb_spectrometer_was_streaming:
                self.usb_spectrometer.resume_streaming()
        
        # emit the acquisition finished signal to enable the UI
        self.processingHandler.end_processing()
        if self.parent is not None:
            try:
                self.parent.dataHandler.set_number_of_images_per_page(self.old_images_per_page)
                self.parent.dataHandler.sort('Sort by prediction score')
                self.parent.dataHandler.signal_populate_page0.emit()
            except:
                pass
        self.acquisitionFinished.emit()
        QApplication.processEvents()

    def request_abort_aquisition(self):
        self.abort_acqusition_requested = True

    def request_job_pause(self):
        """JOBs: pause the running job at the next safe point (between two operations)."""
        self.job_pause_requested = True

    def resume_job(self, new_positions=None):
        """JOBs: continue a paused job. new_positions: list of (x,y,z) from the positions table, or None to keep them."""
        self.job_new_positions = new_positions
        self.job_pause_requested = False

    def slot_detection_stats(self, stats):
        self.detection_stats.emit(stats)

    def slot_image_to_display(self,image):
        self.image_to_display.emit(image)

    def slot_image_to_display_tiled_preview(self,image):
        self.image_to_display_tiled_preview.emit(image)

    def slot_spectrum_to_display(self,data):
        self.spectrum_to_display.emit(data)

    def slot_image_to_display_multi(self,image,illumination_source):
        self.image_to_display_multi.emit(image,illumination_source)

    def slot_current_configuration(self,configuration):
        self.signal_current_configuration.emit(configuration)

    def slot_register_current_fov(self,x_mm,y_mm):
        self.signal_register_current_fov.emit(x_mm,y_mm)

    def slot_napari_layers_update(self, image, i, j, k, channel):
        self.napari_layers_update.emit(image, i, j, k, channel)

    def slot_napari_layers_init(self, image_height, image_width, dtype, rgb):
        self.napari_layers_init.emit(image_height, image_width, dtype, rgb)

    def slot_z_piezo_um(self, displacement_um):
        self.signal_z_piezo_um.emit(displacement_um)


class TrackingController(QObject):

    signal_tracking_stopped = Signal()
    image_to_display = Signal(np.ndarray)
    image_to_display_multi = Signal(np.ndarray,int)
    signal_current_configuration = Signal(Configuration)

    def __init__(self,camera,microcontroller,navigationController,configurationManager,liveController,autofocusController,imageDisplayWindow):
        QObject.__init__(self)
        self.camera = camera
        self.microcontroller = microcontroller
        self.navigationController = navigationController
        self.configurationManager = configurationManager
        self.liveController = liveController
        self.autofocusController = autofocusController
        self.imageDisplayWindow = imageDisplayWindow
        self.tracker = tracking.Tracker_Image()
        # self.tracker_z = tracking.Tracker_Z()
        # self.pid_controller_x = tracking.PID_Controller()
        # self.pid_controller_y = tracking.PID_Controller()
        # self.pid_controller_z = tracking.PID_Controller()

        self.tracking_time_interval_s = 0

        self.crop_width = Acquisition.CROP_WIDTH
        self.crop_height = Acquisition.CROP_HEIGHT
        self.display_resolution_scaling = Acquisition.IMAGE_DISPLAY_SCALING_FACTOR
        self.counter = 0
        self.experiment_ID = None
        self.base_path = None
        self.selected_configurations = []

        self.flag_stage_tracking_enabled = True
        self.flag_AF_enabled = False
        self.flag_save_image = False
        self.flag_stop_tracking_requested = False

        self.pixel_size_um = None
        self.objective = None

    def start_tracking(self):
        
        # save pre-tracking configuration
        print('start tracking')
        self.configuration_before_running_tracking = self.liveController.currentConfiguration
        
        # stop live
        if self.liveController.is_live:
            self.was_live_before_tracking = True
            self.liveController.stop_live() # @@@ to do: also uncheck the live button
        else:
            self.was_live_before_tracking = False

        # disable callback
        if self.camera.callback_is_enabled:
            self.camera_callback_was_enabled_before_tracking = True
            self.camera.disable_callback()
        else:
            self.camera_callback_was_enabled_before_tracking = False

        # hide roi selector
        self.imageDisplayWindow.hide_ROI_selector()

        # run tracking
        self.flag_stop_tracking_requested = False
        # create a QThread object
        try:
            if self.thread.isRunning():
                print('*** previous tracking thread is still running ***')
                self.thread.terminate()
                self.thread.wait()
                print('*** previous tracking threaded manually stopped ***')
        except:
            pass
        self.thread = QThread()
        # create a worker object
        self.trackingWorker = TrackingWorker(self)
        # move the worker to the thread
        self.trackingWorker.moveToThread(self.thread)
        # connect signals and slots
        self.thread.started.connect(self.trackingWorker.run)
        self.trackingWorker.finished.connect(self._on_tracking_stopped)
        self.trackingWorker.finished.connect(self.trackingWorker.deleteLater)
        self.trackingWorker.finished.connect(self.thread.quit)
        self.trackingWorker.image_to_display.connect(self.slot_image_to_display)
        self.trackingWorker.image_to_display_multi.connect(self.slot_image_to_display_multi)
        self.trackingWorker.signal_current_configuration.connect(self.slot_current_configuration,type=Qt.BlockingQueuedConnection)
        # self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.thread.quit)
        # start the thread
        self.thread.start()

    def _on_tracking_stopped(self):

        # restore the previous selected mode
        self.signal_current_configuration.emit(self.configuration_before_running_tracking)

        # re-enable callback
        if self.camera_callback_was_enabled_before_tracking:
            self.camera.enable_callback()
            self.camera_callback_was_enabled_before_tracking = False
        
        # re-enable live if it's previously on
        if self.was_live_before_tracking:
            self.liveController.start_live()

        # show ROI selector
        self.imageDisplayWindow.show_ROI_selector()
        
        # emit the acquisition finished signal to enable the UI
        self.signal_tracking_stopped.emit()
        QApplication.processEvents()

    def start_new_experiment(self,experiment_ID): # @@@ to do: change name to prepare_folder_for_new_experiment
        # generate unique experiment ID
        self.experiment_ID = experiment_ID + '_' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f')
        self.recording_start_time = time.time()
        # create a new folder
        try:
            os.mkdir(os.path.join(self.base_path,self.experiment_ID))
            self.configurationManager.write_configuration(os.path.join(self.base_path,self.experiment_ID)+"/configurations.xml") # save the configuration for the experiment
        except:
            print('error in making a new folder')
            pass

    def set_selected_configurations(self, selected_configurations_name):
        self.selected_configurations = []
        for configuration_name in selected_configurations_name:
            self.selected_configurations.append(next((config for config in self.configurationManager.configurations if config.name == configuration_name)))

    def toggle_stage_tracking(self,state):
        self.flag_stage_tracking_enabled = state > 0
        print('set stage tracking enabled to ' + str(self.flag_stage_tracking_enabled))

    def toggel_enable_af(self,state):
        self.flag_AF_enabled = state > 0
        print('set af enabled to ' + str(self.flag_AF_enabled))

    def toggel_save_images(self,state):
        self.flag_save_image = state > 0
        print('set save images to ' + str(self.flag_save_image))

    def set_base_path(self,path):
        self.base_path = path

    def stop_tracking(self):
        self.flag_stop_tracking_requested = True
        print('stop tracking requested')

    def slot_image_to_display(self,image):
        self.image_to_display.emit(image)

    def slot_image_to_display_multi(self,image,illumination_source):
        self.image_to_display_multi.emit(image,illumination_source)

    def slot_current_configuration(self,configuration):
        self.signal_current_configuration.emit(configuration)

    def update_pixel_size(self, pixel_size_um):
        self.pixel_size_um = pixel_size_um

    def update_tracker_selection(self,tracker_str):
        self.tracker.update_tracker_type(tracker_str)

    def set_tracking_time_interval(self,time_interval):
        self.tracking_time_interval_s = time_interval

    def update_image_resizing_factor(self,image_resizing_factor):
        self.image_resizing_factor = image_resizing_factor
        print('update tracking image resizing factor to ' + str(self.image_resizing_factor))
        self.pixel_size_um_scaled = self.pixel_size_um/self.image_resizing_factor

    # PID-based tracking
    '''
    def on_new_frame(self,image,frame_ID,timestamp):
        # initialize the tracker when a new track is started
        if self.tracking_frame_counter == 0:
            # initialize the tracker
            # initialize the PID controller
            pass

        # crop the image, resize the image 
        # [to fill]

        # get the location
        [x,y] = self.tracker_xy.track(image)
        z = self.track_z.track(image)

        # get motion commands
        dx = self.pid_controller_x.get_actuation(x)
        dy = self.pid_controller_y.get_actuation(y)
        dz = self.pid_controller_z.get_actuation(z)

        # read current location from the microcontroller
        current_stage_position = self.microcontroller.read_received_packet()

        # save the coordinate information (possibly enqueue image for saving here to if a separate ImageSaver object is being used) before the next movement
        # [to fill]

        # generate motion commands
        motion_commands = self.generate_motion_commands(self,dx,dy,dz)

        # send motion commands
        self.microcontroller.send_command(motion_commands)

    def start_a_new_track(self):
        self.tracking_frame_counter = 0
    '''

class TrackingWorker(QObject):

    finished = Signal()
    image_to_display = Signal(np.ndarray)
    image_to_display_multi = Signal(np.ndarray,int)
    signal_current_configuration = Signal(Configuration)

    def __init__(self,trackingController):
        QObject.__init__(self)
        self.trackingController = trackingController

        self.camera = self.trackingController.camera
        self.microcontroller = self.trackingController.microcontroller
        self.navigationController = self.trackingController.navigationController
        self.liveController = self.trackingController.liveController
        self.autofocusController = self.trackingController.autofocusController
        self.configurationManager = self.trackingController.configurationManager
        self.imageDisplayWindow = self.trackingController.imageDisplayWindow
        self.crop_width = self.trackingController.crop_width
        self.crop_height = self.trackingController.crop_height
        self.display_resolution_scaling = self.trackingController.display_resolution_scaling
        self.counter = self.trackingController.counter
        self.experiment_ID = self.trackingController.experiment_ID
        self.base_path = self.trackingController.base_path
        self.selected_configurations = self.trackingController.selected_configurations
        self.tracker = trackingController.tracker
        
        self.number_of_selected_configurations = len(self.selected_configurations)

        # self.tracking_time_interval_s = self.trackingController.tracking_time_interval_s
        # self.flag_stage_tracking_enabled = self.trackingController.flag_stage_tracking_enabled
        # self.flag_AF_enabled = False
        # self.flag_save_image = False
        # self.flag_stop_tracking_requested = False

        self.image_saver = ImageSaver_Tracking(base_path=os.path.join(self.base_path,self.experiment_ID),image_format='bmp')

    def run(self):

        tracking_frame_counter = 0
        t0 = time.time()

        # save metadata
        self.txt_file = open( os.path.join(self.base_path,self.experiment_ID,"metadata.txt"), "w+")
        self.txt_file.write('t0: ' + datetime.now().strftime('%Y-%m-%d_%H-%M-%S.%f') + '\n')
        self.txt_file.write('objective: ' + self.trackingController.objective + '\n')
        self.txt_file.close()

        # create a file for logging 
        self.csv_file = open( os.path.join(self.base_path,self.experiment_ID,"track.csv"), "w+")
        self.csv_file.write('dt (s), x_stage (mm), y_stage (mm), z_stage (mm), x_image (mm), y_image(mm), image_filename\n')

        # reset tracker
        self.tracker.reset()

        # get the manually selected roi
        init_roi = self.imageDisplayWindow.get_roi_bounding_box()
        self.tracker.set_roi_bbox(init_roi)

        # tracking loop
        while self.trackingController.flag_stop_tracking_requested == False:

            print('tracking_frame_counter: ' + str(tracking_frame_counter) )
            if tracking_frame_counter == 0:
                is_first_frame = True
            else:
                is_first_frame = False

            # timestamp
            timestamp_last_frame = time.time()

            # switch to the tracking config
            config = self.selected_configurations[0]
            self.signal_current_configuration.emit(config)
            self.wait_till_operation_is_completed()

            # do autofocus 
            if self.trackingController.flag_AF_enabled and tracking_frame_counter > 1:
                # do autofocus
                print('>>> autofocus')
                self.autofocusController.autofocus()
                self.autofocusController.wait_till_autofocus_has_completed()
                print('>>> autofocus completed')

            # get current position
            x_stage = self.navigationController.x_pos_mm
            y_stage = self.navigationController.y_pos_mm
            z_stage = self.navigationController.z_pos_mm

            # grab an image
            config = self.selected_configurations[0]
            if(self.number_of_selected_configurations > 1):
                self.signal_current_configuration.emit(config)
                self.wait_till_operation_is_completed()
                self.liveController.turn_on_illumination()        # keep illumination on for single configuration acqusition
                self.wait_till_operation_is_completed()
            t = time.time()
            self.camera.send_trigger() 
            image = self.camera.read_frame()
            if(self.number_of_selected_configurations > 1):
                self.liveController.turn_off_illumination()       # keep illumination on for single configuration acqusition
            # image crop, rotation and flip
            image = utils.crop_image(image,self.crop_width,self.crop_height)
            image = np.squeeze(image)
            image = utils.rotate_and_flip_image(image,rotate_image_angle=ROTATE_IMAGE_ANGLE,flip_image=FLIP_IMAGE)
            # get image size
            image_shape = image.shape
            image_center = np.array([image_shape[1]*0.5,image_shape[0]*0.5])

            # image the rest configurations
            for config_ in self.selected_configurations[1:]:
                self.signal_current_configuration.emit(config_)
                self.wait_till_operation_is_completed()
                self.liveController.turn_on_illumination()
                self.wait_till_operation_is_completed()
                self.camera.send_trigger() 
                image_ = self.camera.read_frame()
                self.liveController.turn_off_illumination()
                image_ = utils.crop_image(image_,self.crop_width,self.crop_height)
                image_ = np.squeeze(image_)
                image_ = utils.rotate_and_flip_image(image_,rotate_image_angle=ROTATE_IMAGE_ANGLE,flip_image=FLIP_IMAGE)
                # display image
                # self.image_to_display.emit(cv2.resize(image,(round(self.crop_width*self.display_resolution_scaling), round(self.crop_height*self.display_resolution_scaling)),cv2.INTER_LINEAR))
                image_to_display_ = utils.crop_image(image_,round(self.crop_width*self.liveController.display_resolution_scaling), round(self.crop_height*self.liveController.display_resolution_scaling))
                # self.image_to_display.emit(image_to_display_)
                self.image_to_display_multi.emit(image_to_display_,config_.illumination_source)
                # save image
                if self.trackingController.flag_save_image:
                    if self.camera.is_color:
                        image = cv2.cvtColor(image,cv2.COLOR_RGB2BGR)
                    self.image_saver.enqueue(image_,tracking_frame_counter,str(config_.name))

            # track
            objectFound,centroid,rect_pts = self.tracker.track(image, None, is_first_frame = is_first_frame)
            if objectFound == False:
                print('')
                break
            in_plane_position_error_pixel = image_center - centroid 
            in_plane_position_error_mm = in_plane_position_error_pixel*self.trackingController.pixel_size_um_scaled/1000
            x_error_mm = in_plane_position_error_mm[0]
            y_error_mm = in_plane_position_error_mm[1]

            # display the new bounding box and the image
            self.imageDisplayWindow.update_bounding_box(rect_pts)
            self.imageDisplayWindow.display_image(image)

            # move
            if self.trackingController.flag_stage_tracking_enabled:
                x_correction_usteps = int(x_error_mm/(SCREW_PITCH_X_MM/FULLSTEPS_PER_REV_X/self.navigationController.x_microstepping))
                y_correction_usteps = int(y_error_mm/(SCREW_PITCH_Y_MM/FULLSTEPS_PER_REV_Y/self.navigationController.y_microstepping))
                self.microcontroller.move_x_usteps(TRACKING_MOVEMENT_SIGN_X*x_correction_usteps)
                self.microcontroller.move_y_usteps(TRACKING_MOVEMENT_SIGN_Y*y_correction_usteps) 

            # save image
            if self.trackingController.flag_save_image:
                self.image_saver.enqueue(image,tracking_frame_counter,str(config.name))

            # save position data            
            # self.csv_file.write('dt (s), x_stage (mm), y_stage (mm), z_stage (mm), x_image (mm), y_image(mm), image_filename\n')
            self.csv_file.write(str(t)+','+str(x_stage)+','+str(y_stage)+','+str(z_stage)+','+str(x_error_mm)+','+str(y_error_mm)+','+str(tracking_frame_counter)+'\n')
            if tracking_frame_counter%100 == 0:
                self.csv_file.flush()

            # wait for movement to complete
            self.wait_till_operation_is_completed() # to do - make sure both x movement and y movement are complete

            # wait till tracking interval has elapsed
            while(time.time() - timestamp_last_frame < self.trackingController.tracking_time_interval_s):
                time.sleep(0.005)

            # increament counter 
            tracking_frame_counter = tracking_frame_counter + 1

        # tracking terminated
        self.csv_file.close()
        self.image_saver.close()
        self.finished.emit()

    def wait_till_operation_is_completed(self):
        while self.microcontroller.is_busy():
            time.sleep(SLEEP_TIME_S)


class ImageDisplayWindow(QMainWindow):

    image_click_coordinates = Signal(int, int, int, int)

    def __init__(self, window_title='', draw_crosshairs = False, show_LUT=False, autoLevels=False):
        super().__init__()
        self.setWindowTitle(window_title)
        self.setWindowFlags(self.windowFlags() | Qt.CustomizeWindowHint)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)
        self.widget = QWidget()
        self.show_LUT = show_LUT
        self.autoLevels = autoLevels

        # interpret image data as row-major instead of col-major
        pg.setConfigOptions(imageAxisOrder='row-major')

        self.graphics_widget = pg.GraphicsLayoutWidget()
        self.graphics_widget.view = self.graphics_widget.addViewBox()
        self.graphics_widget.view.invertY()
        
        ## lock the aspect ratio so pixels are always square
        self.graphics_widget.view.setAspectLocked(True)
        
        ## Create image item
        if self.show_LUT:
            self.graphics_widget.view = pg.ImageView()
            self.graphics_widget.img = self.graphics_widget.view.getImageItem()
            self.graphics_widget.img.setBorder('w')
            self.graphics_widget.view.ui.roiBtn.hide()
            self.graphics_widget.view.ui.menuBtn.hide()
            # self.LUTWidget = self.graphics_widget.view.getHistogramWidget()
            # self.LUTWidget.autoHistogramRange()
            # self.graphics_widget.view.autolevels()
        else:
            self.graphics_widget.img = pg.ImageItem(border='w')
            self.graphics_widget.view.addItem(self.graphics_widget.img)

        ## Create ROI
        self.roi_pos = (500,500)
        self.roi_size = (500,500)
        self.ROI = pg.ROI(self.roi_pos, self.roi_size, scaleSnap=True, translateSnap=True)
        self.ROI.setZValue(10)
        self.ROI.addScaleHandle((0,0), (1,1))
        self.ROI.addScaleHandle((1,1), (0,0))
        self.graphics_widget.view.addItem(self.ROI)
        self.ROI.hide()
        self.ROI.sigRegionChanged.connect(self.update_ROI)
        self.roi_pos = self.ROI.pos()
        self.roi_size = self.ROI.size()

        ## Variables for annotating images
        self.draw_rectangle = False
        self.ptRect1 = None
        self.ptRect2 = None
        self.DrawCirc = False
        self.centroid = None
        self.DrawCrossHairs = False
        self.image_offset = np.array([0, 0])

        ## Layout
        layout = QGridLayout()
        if self.show_LUT:
            layout.addWidget(self.graphics_widget.view, 0, 0) 
        else:
            layout.addWidget(self.graphics_widget, 0, 0) 
        self.widget.setLayout(layout)
        self.setCentralWidget(self.widget)

        # set window size
        desktopWidget = QDesktopWidget();
        width = min(desktopWidget.height()*0.9,1000) #@@@TO MOVE@@@#
        height = width
        self.setFixedSize(int(width),int(height))
        if self.show_LUT:
            self.graphics_widget.view.getView().scene().sigMouseClicked.connect(self.mouse_clicked)
        else:
            self.graphics_widget.view.scene().sigMouseClicked.connect(self.mouse_clicked)
        
    def is_within_image(self, coordinates):
        try:
            image_width = self.graphics_widget.img.width()
            image_height = self.graphics_widget.img.height()

            return 0 <= coordinates.x() < image_width and 0 <= coordinates.y() < image_height
        except:
            return False

    def mouse_clicked(self, evt):
        try:
            pos = evt.pos()
            if self.show_LUT:
                view_coord = self.graphics_widget.view.getView().mapSceneToView(pos)
            else:
                view_coord = self.graphics_widget.view.mapSceneToView(pos)
            image_coord = self.graphics_widget.img.mapFromView(view_coord)
        except:
            return

        if self.is_within_image(image_coord):
            x_pixel_centered = int(image_coord.x() - self.graphics_widget.img.width()/2)
            y_pixel_centered = int(image_coord.y() - self.graphics_widget.img.height()/2)
            self.image_click_coordinates.emit(x_pixel_centered, y_pixel_centered, self.graphics_widget.img.width(), self.graphics_widget.img.height()) 

    def display_image(self,image):
        if ENABLE_TRACKING:
            image = np.copy(image)
            self.image_height = image.shape[0],
            self.image_width = image.shape[1]
            if(self.draw_rectangle):
                cv2.rectangle(image, self.ptRect1, self.ptRect2,(255,255,255) , 4)
                self.draw_rectangle = False
            self.graphics_widget.img.setImage(image,autoLevels=self.autoLevels)
        else:
            self.graphics_widget.img.setImage(image,autoLevels=self.autoLevels)

    def update_ROI(self):
        self.roi_pos = self.ROI.pos()
        self.roi_size = self.ROI.size()

    def show_ROI_selector(self):
        self.ROI.show()

    def hide_ROI_selector(self):
        self.ROI.hide()

    def get_roi(self):
        return self.roi_pos,self.roi_size

    def update_bounding_box(self,pts):
        self.draw_rectangle=True
        self.ptRect1=(pts[0][0],pts[0][1])
        self.ptRect2=(pts[1][0],pts[1][1])

    def get_roi_bounding_box(self):
        self.update_ROI()
        width = self.roi_size[0]
        height = self.roi_size[1]
        xmin = max(0, self.roi_pos[0])
        ymin = max(0, self.roi_pos[1])
        return np.array([xmin, ymin, width, height])

    def set_autolevel(self,enabled):
        self.autoLevels = enabled
        print('set autolevel to ' + str(enabled))


class NavigationViewer(QFrame):

    def __init__(self, objectivestore, sample = 'glass slide', invertX = False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)
        self.sample = sample
        self.objectiveStore = objectivestore
        self.well_size_mm = WELL_SIZE_MM
        self.well_spacing_mm = WELL_SPACING_MM
        self.number_of_skip = NUMBER_OF_SKIP
        self.a1_x_mm = A1_X_MM
        self.a1_y_mm = A1_Y_MM
        self.a1_x_pixel = A1_X_PIXEL
        self.a1_y_pixel = A1_Y_PIXEL
        self.location_update_threshold_mm = 0.2
        self.box_color = (255, 0, 0)
        self.box_line_thickness = 2
        self.acquisition_size = Acquisition.CROP_HEIGHT
        self.x_mm = None
        self.y_mm = None

        print("navigation viewer:", sample)
        self.init_ui(invertX)
        self.load_background_image(sample)
        self.update_display_properties(sample)
        self.update_display()

    def init_ui(self, invertX):
        # interpret image data as row-major instead of col-major
        pg.setConfigOptions(imageAxisOrder='row-major')
        self.graphics_widget = pg.GraphicsLayoutWidget()
        self.graphics_widget.setBackground("w")
        self.graphics_widget.view = self.graphics_widget.addViewBox(invertX=invertX,invertY=True)
        ## lock the aspect ratio so pixels are always square
        self.graphics_widget.view.setAspectLocked(True)
        ## Create image item
        self.graphics_widget.img = pg.ImageItem(border='w')
        self.graphics_widget.view.addItem(self.graphics_widget.img)

        self.grid = QVBoxLayout()
        self.grid.addWidget(self.graphics_widget)
        self.setLayout(self.grid)

    def load_background_image(self, sample):
        image_paths = {
            'glass slide': 'images/slide carrier_828x662.png',
            '6 well plate': 'images/6 well plate_1509x1010.png',
            '12 well plate': 'images/12 well plate_1509x1010.png',
            '24 well plate': 'images/24 well plate_1509x1010.png',
            '96 well plate': 'images/96 well plate_1509x1010.png',
            '384 well plate': 'images/384 well plate_1509x1010.png',
            '1536 well plate': 'images/1536 well plate_1509x1010.png'
        }
        self.background_image = cv2.imread(image_paths.get(sample, 'images/slide carrier_828x662.png'))
        self.current_image = np.copy(self.background_image)
        self.current_image_display = np.copy(self.background_image)
        self.image_height = self.background_image.shape[0]
        self.image_width = self.background_image.shape[1]

    def update_display_properties(self, sample):
        if sample == 'glass slide':
            self.location_update_threshold_mm = 0.2
            self.mm_per_pixel = 0.1453
            self.origin_x_pixel = 200
            self.origin_y_pixel = 120
            #self.graphics_widget.view.invertY(False)
        else:
            self.location_update_threshold_mm = 0.05
            self.mm_per_pixel = 0.084665
            self.origin_x_pixel = self.a1_x_pixel - (self.a1_x_mm)/self.mm_per_pixel
            self.origin_y_pixel = self.a1_y_pixel - (self.a1_y_mm)/self.mm_per_pixel
            #self.graphics_widget.view.invertY(True)
        self.update_fov_size()

    def update_fov_size(self):
        self.fov_size_mm = self.acquisition_size * self.objectiveStore.get_pixel_size() / 1000

    def on_objective_changed(self):
        self.clear_slide()
        self.update_fov_size()
        if self.x_mm is not None and self.y_mm is not None:
            self.draw_current_fov(self.x_mm, self.y_mm)
            self.update_display()

    def update_wellplate_settings(self, sample_format, a1_x_mm, a1_y_mm, a1_x_pixel, a1_y_pixel, well_size_mm, well_spacing_mm, number_of_skip):
        if sample_format == 0:
            sample = 'glass slide'
        else:
            sample = f'{sample_format} well plate'
        self.sample = sample
        self.a1_x_mm = a1_x_mm
        self.a1_y_mm = a1_y_mm
        self.a1_x_pixel = a1_x_pixel
        self.a1_y_pixel = a1_y_pixel
        self.well_size_mm = well_size_mm
        self.well_spacing_mm = well_spacing_mm
        self.number_of_skip = number_of_skip
        self.load_background_image(sample)
        self.update_display_properties(sample)
        self.draw_current_fov(self.x_mm,self.y_mm)
        self.update_display()

    def update_current_location(self, x_mm, y_mm):
        if self.x_mm is not None and self.y_mm is not None:
            # update only when the displacement has exceeded certain value
            if abs(x_mm - self.x_mm) > self.location_update_threshold_mm or abs(y_mm - self.y_mm) > self.location_update_threshold_mm:
                self.draw_current_fov(x_mm, y_mm)
                self.update_display()
                self.x_mm = x_mm
                self.y_mm = y_mm
        else:
            self.draw_current_fov(x_mm, y_mm)
            self.update_display()
            self.x_mm = x_mm
            self.y_mm = y_mm

    def get_FOV_pixel_coordinates(self, x_mm, y_mm):
        if self.sample == 'glass slide':
            if INVERTED_OBJECTIVE:
                current_FOV_top_left = (
                    round(self.image_width - (self.origin_x_pixel + x_mm/self.mm_per_pixel - self.fov_size_mm/2/self.mm_per_pixel)),
                    round(self.image_height - (self.origin_y_pixel + y_mm/self.mm_per_pixel) - self.fov_size_mm/2/self.mm_per_pixel)
                )
                current_FOV_bottom_right = (
                    round(self.image_width - (self.origin_x_pixel + x_mm/self.mm_per_pixel + self.fov_size_mm/2/self.mm_per_pixel)),
                    round(self.image_height - (self.origin_y_pixel + y_mm/self.mm_per_pixel) + self.fov_size_mm/2/self.mm_per_pixel)
                )
            else:
                current_FOV_top_left = (
                    round(self.origin_x_pixel + x_mm/self.mm_per_pixel - self.fov_size_mm/2/self.mm_per_pixel),
                    round(self.image_height - (self.origin_y_pixel + y_mm/self.mm_per_pixel) - self.fov_size_mm/2/self.mm_per_pixel)
                )
                current_FOV_bottom_right = (
                    round(self.origin_x_pixel + x_mm/self.mm_per_pixel + self.fov_size_mm/2/self.mm_per_pixel),
                    round(self.image_height - (self.origin_y_pixel + y_mm/self.mm_per_pixel) + self.fov_size_mm/2/self.mm_per_pixel)
                )
        else:
            current_FOV_top_left = (
                round(self.origin_x_pixel + x_mm/self.mm_per_pixel - self.fov_size_mm/2/self.mm_per_pixel),
                round((self.origin_y_pixel + y_mm/self.mm_per_pixel) - self.fov_size_mm/2/self.mm_per_pixel)
            )
            current_FOV_bottom_right = (
                round(self.origin_x_pixel + x_mm/self.mm_per_pixel + self.fov_size_mm/2/self.mm_per_pixel),
                round((self.origin_y_pixel + y_mm/self.mm_per_pixel) + self.fov_size_mm/2/self.mm_per_pixel)
            )
        return current_FOV_top_left, current_FOV_bottom_right

    def draw_current_fov(self,x_mm,y_mm):
        self.current_image_display = np.copy(self.current_image)
        current_FOV_top_left, current_FOV_bottom_right = self.get_FOV_pixel_coordinates(x_mm,y_mm)
        cv2.rectangle(self.current_image_display, current_FOV_top_left, current_FOV_bottom_right, self.box_color, self.box_line_thickness)

    def update_display(self):
        self.graphics_widget.img.setImage(self.current_image_display,autoLevels=False)

    def clear_slide(self):
        self.current_image = np.copy(self.background_image)
        self.current_image_display = np.copy(self.background_image)
        self.update_display()

    def register_fov(self,x_mm,y_mm):
        color = (0,0,255)
        current_FOV_top_left, current_FOV_bottom_right = self.get_FOV_pixel_coordinates(x_mm,y_mm)
        cv2.rectangle(self.current_image, current_FOV_top_left, current_FOV_bottom_right, color, self.box_line_thickness)

    def register_fov_to_image(self,x_mm,y_mm):
        color = (252,174,30)
        current_FOV_top_left, current_FOV_bottom_right = self.get_FOV_pixel_coordinates(x_mm,y_mm)
        cv2.rectangle(self.current_image, current_FOV_top_left, current_FOV_bottom_right, color, self.box_line_thickness)

    def deregister_fov_to_image(self,x_mm,y_mm):
        color = (255,255,255)
        current_FOV_top_left, current_FOV_bottom_right = self.get_FOV_pixel_coordinates(x_mm,y_mm)
        cv2.rectangle(self.current_image, current_FOV_top_left, current_FOV_bottom_right, color, self.box_line_thickness)


class ImageArrayDisplayWindow(QMainWindow):

    def __init__(self, window_title=''):
        super().__init__()
        self.setWindowTitle(window_title)
        self.setWindowFlags(self.windowFlags() | Qt.CustomizeWindowHint)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)
        self.widget = QWidget()

        # interpret image data as row-major instead of col-major
        pg.setConfigOptions(imageAxisOrder='row-major')

        self.graphics_widget_1 = pg.GraphicsLayoutWidget()
        self.graphics_widget_1.view = self.graphics_widget_1.addViewBox()
        self.graphics_widget_1.view.setAspectLocked(True)
        self.graphics_widget_1.img = pg.ImageItem(border='w')
        self.graphics_widget_1.view.addItem(self.graphics_widget_1.img) 
        self.graphics_widget_1.view.invertY()

        self.graphics_widget_2 = pg.GraphicsLayoutWidget()
        self.graphics_widget_2.view = self.graphics_widget_2.addViewBox()
        self.graphics_widget_2.view.setAspectLocked(True)
        self.graphics_widget_2.img = pg.ImageItem(border='w')
        self.graphics_widget_2.view.addItem(self.graphics_widget_2.img)
        self.graphics_widget_2.view.invertY()

        self.graphics_widget_3 = pg.GraphicsLayoutWidget()
        self.graphics_widget_3.view = self.graphics_widget_3.addViewBox()
        self.graphics_widget_3.view.setAspectLocked(True)
        self.graphics_widget_3.img = pg.ImageItem(border='w')
        self.graphics_widget_3.view.addItem(self.graphics_widget_3.img)
        self.graphics_widget_3.view.invertY()

        self.graphics_widget_4 = pg.GraphicsLayoutWidget()
        self.graphics_widget_4.view = self.graphics_widget_4.addViewBox()
        self.graphics_widget_4.view.setAspectLocked(True)
        self.graphics_widget_4.img = pg.ImageItem(border='w')
        self.graphics_widget_4.view.addItem(self.graphics_widget_4.img)
        self.graphics_widget_4.view.invertY()
        ## Layout
        layout = QGridLayout()
        layout.addWidget(self.graphics_widget_1, 0, 0)
        layout.addWidget(self.graphics_widget_2, 0, 1)
        layout.addWidget(self.graphics_widget_3, 1, 0)
        layout.addWidget(self.graphics_widget_4, 1, 1) 
        self.widget.setLayout(layout)
        self.setCentralWidget(self.widget)

        # set window size
        desktopWidget = QDesktopWidget();
        width = min(desktopWidget.height()*0.9,1000) #@@@TO MOVE@@@#
        height = width
        self.setFixedSize(int(width),int(height))

    def display_image(self,image,illumination_source):
        if illumination_source < 11:
            self.graphics_widget_1.img.setImage(image,autoLevels=False)
        elif illumination_source == 11:
            self.graphics_widget_2.img.setImage(image,autoLevels=False)
        elif illumination_source == 12:
            self.graphics_widget_3.img.setImage(image,autoLevels=False)
        elif illumination_source == 13:
            self.graphics_widget_4.img.setImage(image,autoLevels=False)

class ConfigurationManager(QObject):
    configurations_changed = Signal()      # channels added / deleted / renamed / values edited in the Illuminations tab: widgets refresh their lists

    def __init__(self,filename="channel_configurations.xml"):
        QObject.__init__(self)
        self.config_filename = filename
        self.configurations = []
        self.read_configurations()
        
    def save_configurations(self):
        self.write_configuration(self.config_filename)

    def write_configuration(self,filename):
        self.config_xml_tree.write(filename, encoding="utf-8", xml_declaration=True, pretty_print=True)

    def read_configurations(self):
        print('read config')
        if(os.path.isfile(self.config_filename)==False):
            utils_config.generate_default_configuration(self.config_filename)
            print('genenrate default config files')
        self.config_xml_tree = ET.parse(self.config_filename)
        self.config_xml_tree_root = self.config_xml_tree.getroot()
        self.num_configurations = 0
        for mode in self.config_xml_tree_root.iter('mode'):
            self.num_configurations += 1
            print("name:", mode.get('Name'), "color:", self.get_channel_color(mode.get('Name')))
            self.configurations.append(
                Configuration(
                    mode_id = mode.get('ID'),
                    name = mode.get('Name'),
                    color = self.get_channel_color(mode.get('Name')),
                    exposure_time = float(mode.get('ExposureTime')),
                    analog_gain = float(mode.get('AnalogGain')),
                    illumination_source = int(mode.get('IlluminationSource')),
                    illumination_intensity = float(mode.get('IlluminationIntensity')),
                    camera_sn = mode.get('CameraSN'),
                    z_offset = float(mode.get('ZOffset')),
                    pixel_format = mode.get('PixelFormat'),
                    _pixel_format_options = mode.get('_PixelFormat_options'),
                    emission_filter_position = int(mode.get('EmissionFilterPosition', 1))
                )
            )

    def update_configuration(self,configuration_id,attribute_name,new_value):
        conf_list = self.config_xml_tree_root.xpath("//mode[@ID='" + str(configuration_id) + "']")   # exact match (contains() also matched ID 1 in ID 11)
        mode_to_update = conf_list[0]
        mode_to_update.set(attribute_name,str(new_value))
        self.save_configurations()

    def set_all_exposures_without_writing(self, exposure_ms):
        """Startup default: the same exposure time (ms) for all channels. Changes the channel objects and the xml tree in memory
        only; the file is written the next time a channel setting is changed in the GUI."""
        for conf in self.configurations:
            conf.exposure_time = float(exposure_ms)
        for mode in self.config_xml_tree_root.iter('mode'):
            mode.set('ExposureTime', str(exposure_ms))

    def update_configuration_without_writing(self, configuration_id, attribute_name, new_value):
        conf_list = self.config_xml_tree_root.xpath("//mode[@ID='" + str(configuration_id) + "']")   # exact match (contains() also matched ID 1 in ID 11)
        mode_to_update = conf_list[0]
        mode_to_update.set(attribute_name,str(new_value))

    def write_configuration_selected(self,selected_configurations,filename): # to be only used with a throwaway instance
        for conf in self.configurations:
            self.update_configuration_without_writing(conf.id, "Selected", 0)
        for conf in selected_configurations:
            self.update_configuration_without_writing(conf.id, "Selected", 1)
        self.write_configuration(filename)
        for conf in selected_configurations:
            self.update_configuration_without_writing(conf.id, "Selected", 0)

    # ---- Illuminations tab: edit, duplicate, delete, add from the permanent template list ------------------------------------------
    def templates_filename(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.config_filename)), 'channel_templates.xml')

    def read_templates(self):
        """The permanent list of illuminations to add new ones from: channel_templates.xml next to the channel file. It is a copy of the
        channel list as it was when the Illuminations tab was first used (the ones available then) and is never changed afterwards by the
        software (edit the file by hand to change the list). -> list of dicts (xml attributes of each mode)"""
        fn = self.templates_filename()
        if not os.path.isfile(fn):
            try:
                self.config_xml_tree.write(fn, encoding="utf-8", xml_declaration=True, pretty_print=True)
            except Exception as e:
                print('could not write', fn, e)
                return [dict(m.attrib) for m in self.config_xml_tree_root.iter('mode')]
        return [dict(m.attrib) for m in ET.parse(fn).getroot().iter('mode')]

    def template_names(self):
        return [t.get('Name') for t in self.read_templates()]

    def unique_name(self, base):
        names = {c.name for c in self.configurations}
        if base not in names:
            return base
        n = 2
        while f'{base} ({n})' in names:
            n += 1
        return f'{base} ({n})'

    def _new_id(self):
        ids = []
        for c in self.configurations:
            try:
                ids.append(int(c.id))
            except (TypeError, ValueError):
                pass
        return str(max(ids + [0]) + 1)

    def _add_mode(self, attribs):
        mode = ET.SubElement(self.config_xml_tree_root, 'mode')
        for k, v in attribs.items():
            mode.set(k, str(v))
        conf = Configuration(
            mode_id=mode.get('ID'), name=mode.get('Name'), color=self.get_channel_color(mode.get('Name')),
            exposure_time=float(mode.get('ExposureTime')), analog_gain=float(mode.get('AnalogGain')),
            illumination_source=int(mode.get('IlluminationSource')), illumination_intensity=float(mode.get('IlluminationIntensity')),
            camera_sn=mode.get('CameraSN'), z_offset=float(mode.get('ZOffset')), pixel_format=mode.get('PixelFormat'),
            _pixel_format_options=mode.get('_PixelFormat_options'), emission_filter_position=int(mode.get('EmissionFilterPosition', 1)))
        self.configurations.append(conf)
        self.save_configurations()
        self.configurations_changed.emit()
        return conf

    # ---- illumination sets (save / load window of the Illuminations tab) ---------------------------------------------------------
    # core illuminations: cannot be deleted (and keep their name); all others can be deleted at any time
    CORE_NAMES = ('BF LED matrix full', 'DF LED matrix', 'Fluorescence 405 nm Ex', 'Fluorescence 488 nm Ex', 'Fluorescence 561 nm Ex')

    def is_core(self, conf_or_name):
        name = conf_or_name if isinstance(conf_or_name, str) else getattr(conf_or_name, 'name', None)
        return name in self.CORE_NAMES

    REQUIRED_MODE_ATTRIBUTES = ('ID', 'Name', 'ExposureTime', 'AnalogGain', 'IlluminationSource', 'IlluminationIntensity')

    def sets_dir(self):
        """saved illumination sets: one xml file per set (same format as channel_configurations.xml), next to the channel file"""
        return os.path.join(os.path.dirname(os.path.abspath(self.config_filename)), 'illumination_sets')

    @staticmethod
    def safe_set_name(name):
        name = str(name).strip()
        bad = '/\\:*?"<>|'
        if not name or any(ch in name for ch in bad) or name.startswith('.'):
            raise ValueError('a set name must not be empty and must not contain any of  / \\ : * ? " < > |')
        return name

    def list_sets(self):
        d = self.sets_dir()
        if not os.path.isdir(d):
            return []
        return sorted(f[:-4] for f in os.listdir(d) if f.lower().endswith('.xml'))

    def set_path(self, name):
        return os.path.join(self.sets_dir(), self.safe_set_name(name) + '.xml')

    def save_set(self, name, overwrite=False):
        path = self.set_path(name)
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(path)
        os.makedirs(self.sets_dir(), exist_ok=True)
        self.config_xml_tree.write(path, encoding="utf-8", xml_declaration=True, pretty_print=True)
        return path

    def export_file(self, path):
        self.config_xml_tree.write(path, encoding="utf-8", xml_declaration=True, pretty_print=True)

    def import_file_to_set(self, path, name, overwrite=False):
        modes = self.read_set_file(path)      # checks the file
        target = self.set_path(name)
        if os.path.exists(target) and not overwrite:
            raise FileExistsError(target)
        os.makedirs(self.sets_dir(), exist_ok=True)
        ET.parse(path).write(target, encoding="utf-8", xml_declaration=True, pretty_print=True)
        return modes

    def delete_set(self, name):
        os.remove(self.set_path(name))

    @classmethod
    def read_set_file(cls, path):
        """-> list of dicts (attributes of each mode); ValueError if the file is not a usable channel list"""
        try:
            root = ET.parse(path).getroot()
        except Exception as e:
            raise ValueError('not a readable xml file: ' + str(e))
        modes = [dict(m.attrib) for m in root.iter('mode')]
        if not modes:
            raise ValueError('the file contains no illuminations (<mode> lines)')
        ids, names = set(), set()
        for m in modes:
            missing = [a for a in cls.REQUIRED_MODE_ATTRIBUTES if a not in m]
            if missing:
                raise ValueError(f'illumination "{m.get("Name", "?")}" lacks ' + ', '.join(missing))
            try:
                float(m['ExposureTime']); float(m['AnalogGain']); int(m['IlluminationSource']); float(m['IlluminationIntensity'])
            except ValueError:
                raise ValueError(f'illumination "{m["Name"]}" has a value that is not a number')
            if m['ID'] in ids or m['Name'] in names:
                raise ValueError(f'ID or name used twice: "{m["Name"]}" (ID {m["ID"]})')
            ids.add(m['ID']); names.add(m['Name'])
        return modes

    def load_set(self, name):
        return self.load_file(self.set_path(name))

    def load_file(self, path):
        """Replace ALL current illuminations by those of the file; channel_configurations.xml is overwritten (save the current
        ones as a set first if they are needed again). Widgets follow via configurations_changed."""
        self.read_set_file(path)
        tree = ET.parse(path)
        # the core illuminations always stay: those that are missing in the set are kept from the current list (with a free ID)
        root = tree.getroot()
        names = {m.get('Name') for m in root.iter('mode')}
        ids = {m.get('ID') for m in root.iter('mode')}
        for m in list(self.config_xml_tree_root.iter('mode')):
            if m.get('Name') in self.CORE_NAMES and m.get('Name') not in names:
                attribs = dict(m.attrib)
                if attribs['ID'] in ids:
                    attribs['ID'] = str(max([int(i) for i in ids if str(i).isdigit()] + [0]) + 1)
                ids.add(attribs['ID'])
                new_mode = ET.SubElement(root, 'mode')
                for k, v in attribs.items():
                    new_mode.set(k, v)
        self.config_xml_tree = tree
        self.config_xml_tree_root = tree.getroot()
        new = []
        for mode in self.config_xml_tree_root.iter('mode'):
            new.append(Configuration(
                mode_id=mode.get('ID'), name=mode.get('Name'), color=self.get_channel_color(mode.get('Name')),
                exposure_time=float(mode.get('ExposureTime')), analog_gain=float(mode.get('AnalogGain')),
                illumination_source=int(mode.get('IlluminationSource')), illumination_intensity=float(mode.get('IlluminationIntensity')),
                camera_sn=mode.get('CameraSN'), z_offset=float(mode.get('ZOffset', 0.0)), pixel_format=mode.get('PixelFormat'),
                _pixel_format_options=mode.get('_PixelFormat_options'), emission_filter_position=int(mode.get('EmissionFilterPosition', 1))))
        self.configurations[:] = new          # same list object (other code keeps a reference to it)
        self.reload_count = getattr(self, 'reload_count', 0) + 1   # widgets that keep channel IDs map them again by name
        self.num_configurations = len(new)
        self.save_configurations()
        self.configurations_changed.emit()
        return new

    def get_configuration(self, conf_id):
        return next((c for c in self.configurations if str(c.id) == str(conf_id)), None)

    def duplicate_configuration(self, conf_id, new_name=None):
        """Copy of a channel (same illumination source, filter, ...), with a new ID and a unique name; a new line in the xml."""
        mode = self.config_xml_tree_root.xpath("//mode[@ID='" + str(conf_id) + "']")
        if not mode:
            raise KeyError('no channel with ID ' + str(conf_id))
        attribs = dict(mode[0].attrib)
        attribs['ID'] = self._new_id()
        attribs['Name'] = self.unique_name(new_name or attribs['Name'])
        return self._add_mode(attribs)

    def add_from_template(self, template_name):
        """New channel from the permanent template list (see read_templates)."""
        t = next((t for t in self.read_templates() if t.get('Name') == template_name), None)
        if t is None:
            raise KeyError('no template ' + str(template_name))
        attribs = dict(t)
        attribs['ID'] = self._new_id()
        attribs['Name'] = self.unique_name(attribs['Name'])
        return self._add_mode(attribs)

    def delete_configuration(self, conf_id):
        if len(self.configurations) <= 1:
            raise ValueError('the last channel cannot be deleted')
        c = self.get_configuration(conf_id)
        if c is not None and self.is_core(c):
            raise ValueError(f'"{c.name}" is a core illumination and cannot be deleted')
        mode = self.config_xml_tree_root.xpath("//mode[@ID='" + str(conf_id) + "']")
        if not mode:
            raise KeyError('no channel with ID ' + str(conf_id))
        self.config_xml_tree_root.remove(mode[0])
        self.configurations[:] = [c for c in self.configurations if str(c.id) != str(conf_id)]
        self.save_configurations()
        self.configurations_changed.emit()

    def set_values(self, conf_id, name=None, exposure=None, gain=None, intensity=None):
        """Edit a channel (objects stay the same, so widgets that reference them see the change) and write the xml."""
        conf = self.get_configuration(conf_id)
        if conf is None:
            raise KeyError('no channel with ID ' + str(conf_id))
        if name is not None and name != conf.name:
            if self.is_core(conf):
                raise ValueError(f'"{conf.name}" is a core illumination; its name cannot be changed')
            if any(c.name == name for c in self.configurations if c is not conf):
                raise ValueError('a channel with the name "' + name + '" exists already')
            conf.name = name
            conf.color = self.get_channel_color(name)
            self.update_configuration_without_writing(conf.id, 'Name', name)
        if exposure is not None:
            conf.exposure_time = float(exposure)
            self.update_configuration_without_writing(conf.id, 'ExposureTime', exposure)
        if gain is not None:
            conf.analog_gain = float(gain)
            self.update_configuration_without_writing(conf.id, 'AnalogGain', gain)
        if intensity is not None:
            conf.illumination_intensity = float(intensity)
            self.update_configuration_without_writing(conf.id, 'IlluminationIntensity', intensity)
        self.save_configurations()
        self.configurations_changed.emit()

    def get_channel_color(self, channel):
        channel_info = CHANNEL_COLORS_MAP.get(self.extract_wavelength(channel), {'hex': 0xFFFFFF, 'name': 'gray'})
        return channel_info['hex']

    def extract_wavelength(self, name):
        # Split the string and find the wavelength number immediately after "Fluorescence"
        parts = name.split()
        if 'Fluorescence' in parts:
            index = parts.index('Fluorescence') + 1
            if index < len(parts):
                return parts[index].split()[0]  # Assuming 'Fluorescence 488 nm Ex' and taking '488'
        for color in ['R', 'G', 'B']:
            if color in parts or "full_" + color in parts:
                return color
        return None

class PlateReaderNavigationController(QObject):

    signal_homing_complete = Signal()
    signal_current_well = Signal(str)

    def __init__(self,microcontroller):
        QObject.__init__(self)
        self.microcontroller = microcontroller
        self.x_pos_mm = 0
        self.y_pos_mm = 0
        self.z_pos_mm = 0
        self.z_pos = 0
        self.x_microstepping = MICROSTEPPING_DEFAULT_X
        self.y_microstepping = MICROSTEPPING_DEFAULT_Y
        self.z_microstepping = MICROSTEPPING_DEFAULT_Z
        self.column = ''
        self.row = ''

        # to be moved to gui for transparency
        self.microcontroller.set_callback(self.update_pos)

        self.is_homing = False
        self.is_scanning = False

    def move_x_usteps(self,usteps):
        self.microcontroller.move_x_usteps(usteps)

    def move_y_usteps(self,usteps):
        self.microcontroller.move_y_usteps(usteps)

    def move_z_usteps(self,usteps):
        self.microcontroller.move_z_usteps(usteps)

    def move_x_to_usteps(self,usteps):
        self.microcontroller.move_x_to_usteps(usteps)

    def move_y_to_usteps(self,usteps):
        self.microcontroller.move_y_to_usteps(usteps)

    def move_z_to_usteps(self,usteps):
        self.microcontroller.move_z_to_usteps(usteps)

    def moveto(self,column,row):
        if column != '':
            mm_per_ustep_X = SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X)
            x_mm = PLATE_READER.OFFSET_COLUMN_1_MM + (int(column)-1)*PLATE_READER.COLUMN_SPACING_MM
            x_usteps = STAGE_MOVEMENT_SIGN_X*round(x_mm/mm_per_ustep_X)
            self.move_x_to_usteps(x_usteps)
        if row != '':
            mm_per_ustep_Y = SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y)
            y_mm = PLATE_READER.OFFSET_ROW_A_MM + (ord(row) - ord('A'))*PLATE_READER.ROW_SPACING_MM
            y_usteps = STAGE_MOVEMENT_SIGN_Y*round(y_mm/mm_per_ustep_Y)
            self.move_y_to_usteps(y_usteps)

    def moveto_row(self,row):
        # row: int, starting from 0
        mm_per_ustep_Y = SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y)
        y_mm = PLATE_READER.OFFSET_ROW_A_MM + row*PLATE_READER.ROW_SPACING_MM
        y_usteps = round(y_mm/mm_per_ustep_Y)
        self.move_y_to_usteps(y_usteps)

    def moveto_column(self,column):
        # column: int, starting from 0
        mm_per_ustep_X = SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X)
        x_mm = PLATE_READER.OFFSET_COLUMN_1_MM + column*PLATE_READER.COLUMN_SPACING_MM
        x_usteps = round(x_mm/mm_per_ustep_X)
        self.move_x_to_usteps(x_usteps)

    def update_pos(self,microcontroller):
        # get position from the microcontroller
        x_pos, y_pos, z_pos, theta_pos = microcontroller.get_pos()
        self.z_pos = z_pos
        # calculate position in mm or rad
        if USE_ENCODER_X:
            self.x_pos_mm = x_pos*STAGE_POS_SIGN_X*ENCODER_STEP_SIZE_X_MM
        else:
            self.x_pos_mm = x_pos*STAGE_POS_SIGN_X*(SCREW_PITCH_X_MM/(self.x_microstepping*FULLSTEPS_PER_REV_X))
        if USE_ENCODER_Y:
            self.y_pos_mm = y_pos*STAGE_POS_SIGN_Y*ENCODER_STEP_SIZE_Y_MM
        else:
            self.y_pos_mm = y_pos*STAGE_POS_SIGN_Y*(SCREW_PITCH_Y_MM/(self.y_microstepping*FULLSTEPS_PER_REV_Y))
        if USE_ENCODER_Z:
            self.z_pos_mm = z_pos*STAGE_POS_SIGN_Z*ENCODER_STEP_SIZE_Z_MM
        else:
            self.z_pos_mm = z_pos*STAGE_POS_SIGN_Z*(SCREW_PITCH_Z_MM/(self.z_microstepping*FULLSTEPS_PER_REV_Z))
        # check homing status
        if self.is_homing and self.microcontroller.mcu_cmd_execution_in_progress == False:
            self.signal_homing_complete.emit()
        # for debugging
        # print('X: ' + str(self.x_pos_mm) + ' Y: ' + str(self.y_pos_mm))
        # check and emit current position
        column = round((self.x_pos_mm - PLATE_READER.OFFSET_COLUMN_1_MM)/PLATE_READER.COLUMN_SPACING_MM)
        if column >= 0 and column <= PLATE_READER.NUMBER_OF_COLUMNS:
            column = str(column+1)
        else:
            column = ' '
        row = round((self.y_pos_mm - PLATE_READER.OFFSET_ROW_A_MM)/PLATE_READER.ROW_SPACING_MM)
        if row >= 0 and row <= PLATE_READER.NUMBER_OF_ROWS:
            row = chr(ord('A')+row)
        else:
            row = ' '

        if self.is_scanning:
            self.signal_current_well.emit(row+column)

    def home(self):
        self.is_homing = True
        self.microcontroller.home_xy()

    def home_x(self):
        self.microcontroller.home_x()

    def home_y(self):
        self.microcontroller.home_y()

class ScanCoordinates(object):
    def __init__(self):
        self.coordinates_mm = []
        self.name = []
        self.well_selector = None
        self.a1_x_mm = A1_X_MM
        self.a1_y_mm = A1_Y_MM
        self.wellplate_offset_x_mm = WELLPLATE_OFFSET_X_mm
        self.wellplate_offset_y_mm = WELLPLATE_OFFSET_Y_mm
        self.well_spacing_mm = WELL_SPACING_MM

    def _index_to_row(self,index):
        index += 1
        row = ""
        while index > 0:
            index -= 1
            row = chr(index % 26 + ord('A')) + row
            index //= 26
        return row

    def add_well_selector(self,well_selector):
        self.well_selector = well_selector

    def update_wellplate_settings(self, format_, a1_x_mm, a1_y_mm, a1_x_pixel, a1_y_pixel, size_mm, spacing_mm, number_of_skip):
        self.format = format_
        self.a1_x_mm = a1_x_mm
        self.a1_y_mm = a1_y_mm
        self.a1_x_pixel = a1_x_pixel
        self.a1_y_pixel = a1_y_pixel
        self.well_size_mm = size_mm
        self.well_spacing_mm = spacing_mm
        self.number_of_skip = number_of_skip

    def get_selected_wells(self):
        # get selected wells from the widget
        selected_wells = self.well_selector.get_selected_cells()
        selected_wells = np.array(selected_wells)
        # clear the previous selection
        self.coordinates_mm = []
        self.name = []
        if len(selected_wells) == 0:
            return False # if no well selected
        # populate the coordinates
        rows = np.unique(selected_wells[:,0])
        _increasing = True
        for row in rows:
            items = selected_wells[selected_wells[:,0]==row]
            columns = items[:,1]
            columns = np.sort(columns)
            if _increasing==False:
                columns = np.flip(columns)
            for column in columns:
                x_mm = self.a1_x_mm + column*self.well_spacing_mm + self.wellplate_offset_x_mm
                y_mm = self.a1_y_mm + row*self.well_spacing_mm + self.wellplate_offset_y_mm
                print("Scan Coordinates:", (x_mm, y_mm))
                self.coordinates_mm.append((x_mm,y_mm))
                self.name.append(self._index_to_row(row)+str(column+1))
            _increasing = not _increasing
        return len(selected_wells) # if wells selected


class LaserAutofocusController(QObject):

    image_to_display = Signal(np.ndarray)
    signal_displacement_um = Signal(float)

    def __init__(self,microcontroller,camera,liveController,navigationController,has_two_interfaces=True,use_glass_top=True, look_for_cache=True):
        QObject.__init__(self)
        self.microcontroller = microcontroller
        self.camera = camera
        self.liveController = liveController
        self.navigationController = navigationController

        self.is_initialized = False
        self.x_reference = 0
        self.pixel_to_um = 1
        self.x_offset = 0
        self.y_offset = 0
        self.x_width = 3088
        self.y_width = 2064

        self.has_two_interfaces = has_two_interfaces # e.g. air-glass and glass water, set to false when (1) using oil immersion (2) using 1 mm thick slide (3) using metal coated slide or Si wafer
        self.use_glass_top = use_glass_top
        self.spot_spacing_pixels = None # spacing between the spots from the two interfaces (unit: pixel)
        
        self.look_for_cache = look_for_cache

        self.image = None # for saving the focus camera image for debugging when centroid cannot be found

        if look_for_cache:
            cache_path = "cache/laser_af_reference_plane.txt"
            try:
                with open(cache_path, "r") as cache_file:
                    for line in cache_file:
                        value_list = line.split(",")
                        x_offset = float(value_list[0])
                        y_offset = float(value_list[1])
                        width = int(value_list[2])
                        height = int(value_list[3])
                        pixel_to_um = float(value_list[4])
                        x_reference = float(value_list[5])
                        self.initialize_manual(x_offset,y_offset,width,height,pixel_to_um,x_reference)
                        break
            except (FileNotFoundError, ValueError,IndexError) as e:
                print("Unable to read laser AF state cache, exception below:")
                print(e)
                pass

    def initialize_manual(self, x_offset, y_offset, width, height, pixel_to_um, x_reference, write_to_cache=True):
        cache_string = ",".join([str(x_offset),str(y_offset), str(width),str(height), str(pixel_to_um), str(x_reference)])
        if write_to_cache:
            cache_path = Path("cache/laser_af_reference_plane.txt")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(cache_string)
        # x_reference is relative to the full sensor
        self.pixel_to_um = pixel_to_um
        self.x_offset = int((x_offset//8)*8)
        self.y_offset = int((y_offset//2)*2)
        self.width = int((width//8)*8)
        self.height = int((height//2)*2)
        self.x_reference = x_reference - self.x_offset # self.x_reference is relative to the cropped region
        self.camera.set_ROI(self.x_offset,self.y_offset,self.width,self.height)
        self.is_initialized = True

    def initialize_auto(self):

        # first find the region to crop
        # then calculate the convert factor

        # set camera to use full sensor
        self.camera.set_ROI(0,0,None,None) # set offset first
        self.camera.set_ROI(0,0,3088,2064)
        # update camera settings
        self.camera.set_exposure_time(FOCUS_CAMERA_EXPOSURE_TIME_MS)
        self.camera.set_analog_gain(FOCUS_CAMERA_ANALOG_GAIN)
        
        # turn on the laser
        self.microcontroller.turn_on_AF_laser()
        self.wait_till_operation_is_completed()

        # get laser spot location
        x,y = self._get_laser_spot_centroid()

        # turn off the laser
        self.microcontroller.turn_off_AF_laser()
        self.wait_till_operation_is_completed()

        x_offset = x - LASER_AF_CROP_WIDTH/2
        y_offset = y - LASER_AF_CROP_HEIGHT/2
        print('laser spot location on the full sensor is (' + str(int(x)) + ',' + str(int(y)) + ')')

        # set camera crop
        self.initialize_manual(x_offset, y_offset, LASER_AF_CROP_WIDTH, LASER_AF_CROP_HEIGHT, 1, x)

        # turn on laser 
        self.microcontroller.turn_on_AF_laser()
        self.wait_till_operation_is_completed()

        # move z to - 6 um
        self.navigationController.move_z(-0.018)
        self.wait_till_operation_is_completed()
        self.navigationController.move_z(0.012)
        self.wait_till_operation_is_completed()
        time.sleep(0.02)

        # measure
        x0,y0 = self._get_laser_spot_centroid()

        # move z to 6 um
        self.navigationController.move_z(0.006)
        self.wait_till_operation_is_completed()
        time.sleep(0.02)

        # measure
        x1,y1 = self._get_laser_spot_centroid()

        # turn off laser
        self.microcontroller.turn_off_AF_laser()
        self.wait_till_operation_is_completed()

        if x1-x0 == 0:
            # for simulation
             self.pixel_to_um = 0.4
        else:
            # calculate the conversion factor
            self.pixel_to_um = 6.0/(x1-x0)
        print('pixel to um conversion factor is ' + str(self.pixel_to_um) + ' um/pixel')

        # set reference
        self.x_reference = x1

        if self.look_for_cache:
            cache_path = "cache/laser_af_reference_plane.txt"
            try:
                x_offset = None
                y_offset = None
                width = None
                height = None
                pixel_to_um = None
                x_reference = None
                with open(cache_path, "r") as cache_file:
                    for line in cache_file:
                        value_list = line.split(",")
                        x_offset = float(value_list[0])
                        y_offset = float(value_list[1])
                        width = int(value_list[2])
                        height = int(value_list[3])
                        pixel_to_um = self.pixel_to_um
                        x_reference = self.x_reference+self.x_offset
                        break
                cache_string = ",".join([str(x_offset),str(y_offset), str(width),str(height), str(pixel_to_um), str(x_reference)])
                cache_path = Path("cache/laser_af_reference_plane.txt")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(cache_string)
            except (FileNotFoundError, ValueError,IndexError) as e:
                print("Unable to read laser AF state cache, exception below:")
                print(e)
                pass

    def measure_displacement(self):
        # turn on the laser
        self.microcontroller.turn_on_AF_laser()
        self.wait_till_operation_is_completed()
        # get laser spot location
        x,y = self._get_laser_spot_centroid()
        # turn off the laser
        self.microcontroller.turn_off_AF_laser()
        self.wait_till_operation_is_completed()
        # calculate displacement
        displacement_um = (x - self.x_reference)*self.pixel_to_um
        self.signal_displacement_um.emit(displacement_um)
        return displacement_um

    def move_to_target(self,target_um):
        current_displacement_um = self.measure_displacement()
        um_to_move = target_um - current_displacement_um
        # limit the range of movement
        um_to_move = min(um_to_move,200)
        um_to_move = max(um_to_move,-200)
        self.navigationController.move_z(um_to_move/1000)
        self.wait_till_operation_is_completed()
        # update the displacement measurement
        self.measure_displacement()

    def set_reference(self):
        # turn on the laser
        self.microcontroller.turn_on_AF_laser()
        self.wait_till_operation_is_completed()
        # get laser spot location
        x,y = self._get_laser_spot_centroid()
        # turn off the laser
        self.microcontroller.turn_off_AF_laser()
        self.wait_till_operation_is_completed()
        self.x_reference = x
        self.signal_displacement_um.emit(0)

    def _caculate_centroid(self,image):
        if self.has_two_interfaces == False:
            h,w = image.shape
            x,y = np.meshgrid(range(w),range(h))
            I = image.astype(float)
            I = I - np.amin(I)
            I[I/np.amax(I)<0.2] = 0
            x = np.sum(x*I)/np.sum(I)
            y = np.sum(y*I)/np.sum(I)
            return x,y
        else:
            I = image
            # get the y position of the spots
            tmp = np.sum(I,axis=1)
            y0 = np.argmax(tmp)
            # crop along the y axis
            I = I[y0-96:y0+96,:]
            # signal along x
            tmp = np.sum(I,axis=0)
            # find peaks
            peak_locations,_ = scipy.signal.find_peaks(tmp,distance=100)
            idx = np.argsort(tmp[peak_locations])
            peak_0_location = peak_locations[idx[-1]]
            peak_1_location = peak_locations[idx[-2]] # for air-glass-water, the smaller peak corresponds to the glass-water interface
            self.spot_spacing_pixels = peak_1_location-peak_0_location
            '''
            # find peaks - alternative
            if self.spot_spacing_pixels is not None:
                peak_locations,_ = scipy.signal.find_peaks(tmp,distance=100)
                idx = np.argsort(tmp[peak_locations])
                peak_0_location = peak_locations[idx[-1]]
                peak_1_location = peak_locations[idx[-2]] # for air-glass-water, the smaller peak corresponds to the glass-water interface
                self.spot_spacing_pixels = peak_1_location-peak_0_location
            else:
                peak_0_location = np.argmax(tmp)
                peak_1_location = peak_0_location + self.spot_spacing_pixels
            '''
            # choose which surface to use
            if self.use_glass_top:
                x1 = peak_1_location
            else:
                x1 = peak_0_location
            # find centroid
            h,w = I.shape
            x,y = np.meshgrid(range(w),range(h))
            I = I[:,max(0,x1-64):min(w-1,x1+64)]
            x = x[:,max(0,x1-64):min(w-1,x1+64)]
            y = y[:,max(0,x1-64):min(w-1,x1+64)]
            I = I.astype(float)
            I = I - np.amin(I)
            I[I/np.amax(I)<0.1] = 0
            x1 = np.sum(x*I)/np.sum(I)
            y1 = np.sum(y*I)/np.sum(I)
            return x1,y0-96+y1

    def _get_laser_spot_centroid(self):
        # disable camera callback
        self.camera.disable_callback()
        tmp_x = 0
        tmp_y = 0
        for i in range(LASER_AF_AVERAGING_N):
            # send camera trigger
            if self.liveController.trigger_mode == TriggerMode.SOFTWARE:            
                self.camera.send_trigger()
            elif self.liveController.trigger_mode == TriggerMode.HARDWARE:
                # self.microcontroller.send_hardware_trigger(control_illumination=True,illumination_on_time_us=self.camera.exposure_time*1000)
                pass # to edit
            # read camera frame
            image = self.camera.read_frame()
            self.image = image
            # optionally display the image
            if LASER_AF_DISPLAY_SPOT_IMAGE:
                self.image_to_display.emit(image)
            # calculate centroid
            x,y = self._caculate_centroid(image)
            tmp_x = tmp_x + x
            tmp_y = tmp_y + y
        x = tmp_x/LASER_AF_AVERAGING_N
        y = tmp_y/LASER_AF_AVERAGING_N
        return x,y

    def wait_till_operation_is_completed(self):
        while self.microcontroller.is_busy():
            time.sleep(SLEEP_TIME_S)

    def get_image(self):
        # turn on the laser
        self.microcontroller.turn_on_AF_laser()
        self.wait_till_operation_is_completed()
        # send trigger, grab image and display image
        self.camera.send_trigger()
        image = self.camera.read_frame()
        self.image_to_display.emit(image)
        # turn off the laser
        self.microcontroller.turn_off_AF_laser()
        self.wait_till_operation_is_completed()
        return image
