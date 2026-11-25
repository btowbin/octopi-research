import argparse
import cv2
import time
import numpy as np
import PySpin
from control._def import *

class ReadType:
    """
    Use the following constants to determine whether nodes are read
    as Value nodes or their individual types.
    """
    VALUE = 0,
    INDIVIDUAL = 1

try:
    if CHOSEN_READ == 'VALUE':
        CHOSEN_READ = ReadType.VALUE
    else:
        CHOSEN_READ = ReadType.INDIVIDUAL
except:
    CHOSEN_READ = ReadType.INDIVIDUAL

def get_value_node(node):
    """
    Retrieves and prints the display name and value of all node types as value nodes.
    A value node is a general node type that allows for the reading and writing of any node type as a string.

    :param node: Node to get information from.
    :type node: INode
    :param level: Depth to indent output.
    :return: node name and value, both strings
    :rtype: (str (node name),str (node value)
    """
    try:
        # Create value node
        node_value = PySpin.CValuePtr(node)

        # Retrieve display name
        #
        # *** NOTES ***
        # A node's 'display name' is generally more appropriate for output and
        # user interaction whereas its 'name' is what the camera understands.
        # Generally, its name is the same as its display name but without
        # spaces - for instance, the name of the node that houses a camera's
        # serial number is 'DeviceSerialNumber' while its display name is
        # 'Device Serial Number'.
        name = node_value.GetName()

        # Retrieve value of any node type as string
        #
        # *** NOTES ***
        # Because value nodes return any node type as a string, it can be much
        # easier to deal with nodes as value nodes rather than their actual
        # individual types.
        value = node_value.ToString()
        return (name,value)
    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)


def get_string_node(node):
    """
    Retrieves the display name and value of a string node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str,str)
    """
    try:
        # Create string node
        node_string = PySpin.CStringPtr(node)

        # Retrieve string node value
        #
        # *** NOTES ***
        # Functions in Spinnaker C++ that use gcstring types
        # are substituted with Python strings in PySpin.
        # The only exception is shown in the DeviceEvents example, where
        # the callback function still uses a wrapped gcstring type.
        name = node_string.GetName()

        # Ensure that the value length is not excessive for printing
        value = node_string.GetValue()

        # Print value; 'level' determines the indentation level of output
        return(name,value)

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)

def get_integer_node(node):
    """
    Retrieves and prints the display name and value of an integer node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str, int)
    """
    try:
        # Create integer node
        node_integer = PySpin.CIntegerPtr(node)

        # Get display name
        name = node_integer.GetName()

        # Retrieve integer node value
        #
        # *** NOTES ***
        # All node types except base nodes have a ToString()
        # method which returns a value as a string.
        value = node_integer.GetValue()

        # Print value
        return (name,value)

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)

def get_float_node(node):
    """
    Retrieves the name and value of a float node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str, float)
    """
    try:

        # Create float node
        node_float = PySpin.CFloatPtr(node)

        # Get display name
        name = node_float.GetName()

        # Retrieve float value
        value = node_float.GetValue()

        # Print value
        return (name,value)

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)


def get_boolean_node(node):
    """
    Retrieves the display name and value of a Boolean node.

    :param node: Node to get information from.
    :type node: INode
    :return: Tuple of node name and value
    :rtype: (str, bool)
    """
    try:
        # Create Boolean node
        node_boolean = PySpin.CBooleanPtr(node)

        # Get display name
        name = node_boolean.GetName()

        # Retrieve Boolean value
        value = node_boolean.GetValue()

        # Print Boolean value
        # NOTE: In Python a Boolean will be printed as "True" or "False".
        return (name,value)

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)


def get_command_node(node):
    """
    This function retrieves the name and tooltip of a command
    The tooltip is printed below because command nodes do not have an intelligible
    value.

    :param node: Node to get information from.
    :type node: INode
    :return: node name and tooltip as a tuple
    :rtype: (str, str)
    """
    try:
        result = True

        # Create command node
        node_command = PySpin.CCommandPtr(node)

        # Get display name
        name = node_command.GetName()

        # Retrieve tooltip
        #
        # *** NOTES ***
        # All node types have a tooltip available. Tooltips provide useful
        # information about nodes. Command nodes do not have a method to
        # retrieve values as their is no intelligible value to retrieve.
        tooltip = node_command.GetToolTip()

        # Ensure that the value length is not excessive for printing

        # Print display name and tooltip
        return (name, tooltip)

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)


def get_enumeration_node_and_current_entry(node):
    """
    This function retrieves and prints the display names of an enumeration node
    and its current entry (which is actually housed in another node unto itself).

    :param node: Node to get information from.
    :type node: INode
    :return: name and symbolic of current entry in enumeration
    :rtype: (str,str)
    """
    try:
        # Create enumeration node
        node_enumeration = PySpin.CEnumerationPtr(node)

        # Retrieve current entry as enumeration node
        #
        # *** NOTES ***
        # Enumeration nodes have three methods to differentiate between: first,
        # GetIntValue() returns the integer value of the current entry node;
        # second, GetCurrentEntry() returns the entry node itself; and third,
        # ToString() returns the symbolic of the current entry.
        node_enum_entry = PySpin.CEnumEntryPtr(node_enumeration.GetCurrentEntry())

        # Get display name
        name = node_enumeration.GetName()

        # Retrieve current symbolic
        #
        # *** NOTES ***
        # Rather than retrieving the current entry node and then retrieving its
        # symbolic, this could have been taken care of in one step by using the
        # enumeration node's ToString() method.
        entry_symbolic = node_enum_entry.GetSymbolic()

        # Print current entry symbolic
        return(name, entry_symbolic)

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
        return ('',None)


def get_category_node_and_all_features(node):
    """
    This function retrieves and prints out the display name of a category node
    before printing all child nodes. Child nodes that are also category nodes
    are also retrieved recursively

    :param node: Category node to get information from.
    :type node: INode
    :return: Dictionary of category node features
    :rtype: dict
    """
    return_dict = {}
    try:
        # Create category node
        node_category = PySpin.CCategoryPtr(node)

        # Get and print display name
        # Retrieve and iterate through all children
        #
        # *** NOTES ***
        # The two nodes that typically have children are category nodes and
        # enumeration nodes. Throughout the examples, the children of category nodes
        # are referred to as features while the children of enumeration nodes are
        # referred to as entries. Keep in mind that enumeration nodes can be cast as
        # category nodes, but category nodes cannot be cast as enumerations.
        for node_feature in node_category.GetFeatures():

            # Ensure node is readable
            if not PySpin.IsReadable(node_feature):
                continue
            
            # Category nodes must be dealt with separately in order to retrieve subnodes recursively.
            if node_feature.GetPrincipalInterfaceType() == PySpin.intfICategory:
                return_dict[PySpin.CCategoryPtr(node_feature).GetName()] = get_category_node_and_all_features(node_feature)

            # Cast all non-category nodes as value nodes
            #
            # *** NOTES ***
            # If dealing with a variety of node types and their values, it may be
            # simpler to cast them as value nodes rather than as their individual types.
            # However, with this increased ease-of-use, functionality is sacrificed.
            elif CHOSEN_READ == ReadType.VALUE:
                node_name, node_value =  get_value_node(node_feature)
                return_dict[node_name] = node_value

            # Cast all non-category nodes as actual types
            elif CHOSEN_READ == ReadType.INDIVIDUAL:
                node_name = ''
                node_value = None
                if node_feature.GetPrincipalInterfaceType() == PySpin.intfIString:
                    node_name, node_value = get_string_node(node_feature)
                elif node_feature.GetPrincipalInterfaceType() == PySpin.intfIInteger:
                    node_name, node_value = get_integer_node(node_feature)
                elif node_feature.GetPrincipalInterfaceType() == PySpin.intfIFloat:
                    node_name, node_value = get_float_node(node_feature)
                elif node_feature.GetPrincipalInterfaceType() == PySpin.intfIBoolean:
                    node_name, node_value= get_boolean_node(node_feature)
                elif node_feature.GetPrincipalInterfaceType() == PySpin.intfICommand:
                    node_name, node_value =  get_command_node(node_feature)
                elif node_feature.GetPrincipalInterfaceType() == PySpin.intfIEnumeration:
                    node_name, node_value = get_enumeration_node_and_current_entry(node_feature)
                return_dict[node_name] = node_value

    except PySpin.SpinnakerException as ex:
        print('Error: %s' % ex)
    
    return return_dict 


def get_device_info(cam):
    nodemap_tldevice = cam.GetTLDeviceNodeMap()
    device_info_dict = {}
    device_info_dict['TLDevice'] = get_category_node_and_all_features(nodemap_tldevice.GetNode('Root'))
    return device_info_dict

def get_device_info_full(cam, get_genicam=False):
    device_info_dict = {}
    nodemap_gentl = cam.GetTLDeviceNodeMap()
    device_info_dict['TLDevice'] = get_category_node_and_all_features(nodemap_gentl.GetNode('Root'))

    nodemap_tlstream = cam.GetTLStreamNodeMap()
    device_info_dict['TLStream'] = get_category_node_and_all_features(nodemap_tlstream.GetNode('Root'))
    if get_genicam:
        cam.Init()

        nodemap_applayer = cam.GetNodeMap()
        device_info_dict['GenICam'] = get_category_node_and_all_features(nodemap_applayer.GetNode('Root'))

        cam.DeInit()
    return device_info_dict

def retrieve_all_camera_info(get_genicam=False):
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    device_num = cam_list.GetSize()
    return_list = []
    if device_num > 0:
        for i,cam in enumerate(cam_list):
            return_list.append(get_device_info_full(cam,get_genicam=get_genicam))
        try:
            del cam
        except NameError:
            pass
    cam_list.Clear()
    system.ReleaseInstance()
    return return_list
  


def get_sn_by_model(model_name):
    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    device_num = cam_list.GetSize()
    sn_to_return = None
    if device_num > 0:
        for i,cam in enumerate(cam_list):
            device_info = get_device_info(cam)
            try:
                if device_info['TLDevice']['DeviceInformation']['DeviceModelName'] == model_name:
                    sn_to_return = device_info['TLDevice']['DeviceInformation']['DeviceSerialNumber']
                    break
            except KeyError:
                pass
        try:
            del cam
        except NameError:
            pass
    cam_list.Clear()
    system.ReleaseInstance()
    return sn_to_return

class ImageEventHandler(PySpin.ImageEventHandler):
    def __init__(self,parent):
        super(ImageEventHandler,self).__init__()

        self.camera = parent #Camera() type object

        self._processor = PySpin.ImageProcessor()
        self._processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

    def OnImageEvent(self, raw_image):
        
        if raw_image.IsIncomplete():
            print('Image incomplete with image status %i ...' % raw_image.GetImageStatus())
            return
        elif self.camera.is_color and 'mono' not in self.camera.pixel_format.lower():
            if "10" in self.camera.pixel_format or "12" in self.camera.pixel_format or "14" in self.camera.pixel_format or "16" in self.camera.pixel_format:
                rgb_image = self._processor.Convert(raw_image,PySpin.PixelFormat_RGB16)
            else:
                rgb_image = self._processor.Convert(raw_image,PySpin.PixelFormat_RGB8)
            numpy_image = rgb_image.GetNDArray()
        else:
            if self.camera.convert_pixel_format:
                converted_image = self._processor.Convert(raw_image,self.camera.conversion_pixel_format)
                numpy_image = converted_image.GetNDArray()
                if self.camera.conversion_pixel_format == PySpin.PixelFormat_Mono12:
                    numpy_image = numpy_image << 4
            else:
                try:
                    numpy_image = raw_image.GetNDArray()
                except PySpin.SpinnakerException:
                    converted_image = self.one_frame_post_processor.Convert(raw_image, PySpin.PixelFormat_Mono8)
                    numpy_image = converted_image.GetNDArray()
                if self.camera.pixel_format == 'MONO12':
                    numpy_image = numpy_image <<4
        self.camera.current_frame = numpy_image
        self.camera.frame_ID_software = self.camera.frame_ID_software + 1
        self.camera.frame_ID = raw_image.GetFrameID()
        if self.camera.trigger_mode == TriggerMode.HARDWARE:
            if self.camera.frame_ID_offset_hardware_trigger == None:
                self.camera.frame_ID_offset_hardware_trigger = self.camera.frame_ID
            self.camera.frame_ID = self.camera.frame_ID - self.camera.frame_ID_offset_hardware_trigger
        self.camera.timestamp = time.time()
        self.camera.new_image_callback_external(self.camera)
       

class Camera(object):

    def __init__(self,sn=None,is_global_shutter=False,rotate_image_angle=None,flip_image=None, is_color=False):

        self.py_spin_system = PySpin.System.GetInstance()
        self.camera_list = self.py_spin_system.GetCameras()
        self.sn = sn 
        self_is_color = is_color
        # many to be purged
        self.is_global_shutter = is_global_shutter
        self.device_info_dict = None
        self.device_index = 0
        self.camera = None #PySpin CameraPtr type
        self.is_color = None
        self.gamma_lut = None
        self.contrast_lut = None
        self.color_correction_param = None

        self.one_frame_post_processor = PySpin.ImageProcessor()
        self.conversion_pixel_format = PySpin.PixelFormat_Mono8
        self.convert_pixel_format = False
        self.one_frame_post_processor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)

        self.auto_exposure_mode =None
        self.auto_gain_mode = None
        self.auto_wb_mode = None
        self.auto_wb_profile = None

        self.rotate_image_angle = rotate_image_angle
        self.flip_image = flip_image

        self.exposure_time = 1 # unit: ms
        # towbinlab: the camera uses the exposure that was written BEFORE THE PREVIOUS trigger (measured on a Blackfly S: hardware_exposure_lag_check.py,
        # every frame carried the exposure of the frame before). Gain is not affected. Fix: after an exposure change, one dark throw-away frame
        # (see _flush_exposure_lag). _written_us = exposure last written, _latched_us = the one the camera will use for the next frame (None = unknown)
        self.exposure_lag_fix = False   # optional (tick 'Empty frame in between' in the Camera tab); see acq_summary.check_exposure_lag
        self._written_us = None
        self._latched_us = None
        self._flushing = False
        self.dummy_trigger = None     # hardware trigger mode: function that sends one camera pulse WITHOUT illumination (set by the LiveController)
        self.analog_gain = 0
        self.frame_ID = -1
        self.frame_ID_software = -1
        self.frame_ID_offset_hardware_trigger = 0
        self.timestamp = 0

        self.image_locked = False
        self.current_frame = None

        self.callback_is_enabled = False
        self.is_streaming = False

        self.GAIN_MAX = 24
        self.GAIN_MIN = 0
        self.GAIN_STEP = 1
        self.EXPOSURE_TIME_MS_MIN = 0.01
        self.EXPOSURE_TIME_MS_MAX = 4000

        self.trigger_mode = None
        self.pixel_size_byte = 1

        # below are values for IMX226 (MER2-1220-32U3M) - to make configurable 
        # (towbinlab: only used as a fallback; the rolling-shutter readout is now measured on the camera itself,
        #  see _update_readout_timing - for the IMX178 these values made the hardware-trigger exposure far too long)
        self.row_period_us = 10
        self.row_numbers = 3036
        self.exposure_delay_us_8bit = 650
        self.exposure_delay_us = self.exposure_delay_us_8bit*self.pixel_size_byte
        self.readout_us = None # measured readout of the current ROI / bit depth (None: use the constants above)
        self.strobe_delay_us = int(self.exposure_delay_us + self._readout_time_us())

        self.pixel_format = None # use the default pixel format

        self.is_live = False # this determines whether a new frame received will be handled in the streamHandler

        self.image_event_handler = ImageEventHandler(self)
        # mainly for discarding the last frame received after stop_live() is called, where illumination is being turned off during exposure

    def open(self,index=0, is_color=None):
        if is_color is None:
            is_color = self.is_color
        try:
            self.camera.DeInit()
            del self.camera
        except AttributeError:
            pass
        self.camera_list.Clear()
        self.camera_list = self.py_spin_system.GetCameras()
        device_num = self.camera_list.GetSize()
        if device_num == 0:
            raise RuntimeError('Could not find any USB camera devices!')
        if self.sn is None:
            self.device_index = index
            self.camera = self.camera_list.GetByIndex(index)
        else:
            self.camera = self.camera_list.GetBySerial(str(self.sn))


        self.device_info_dict = get_device_info_full(self.camera, get_genicam=True)
        
        self.camera.Init()
        self.nodemap = self.camera.GetNodeMap()
        
        self.is_color = is_color
        if self.is_color:
            self.set_wb_ratios(2,1,2)


        # set to highest possible framerate
        PySpin.CBooleanPtr(self.nodemap.GetNode('AcquisitionFrameRateEnable')).SetValue(True)
        target_rate = 1000
        for decrement in range(0,1000):
            try:
                PySpin.CFloatPtr(self.nodemap.GetNode('AcquisitionFrameRate')).SetValue(target_rate-decrement)
                break
            except PySpin.SpinnakerException as ex:
                pass

        # turn off device throughput limit
        node_throughput_limit =  PySpin.CIntegerPtr(self.nodemap.GetNode('DeviceLinkThroughputLimit'))
        node_throughput_limit.SetValue(node_throughput_limit.GetMax())

        self.Width = PySpin.CIntegerPtr(self.nodemap.GetNode('Width')).GetValue()
        self.Height = PySpin.CIntegerPtr(self.nodemap.GetNode('Height')).GetValue()
        

        self.WidthMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode('SensorWidth')).GetValue()
        self.HeightMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode('SensorHeight')).GetValue()
        
        self.set_ROI(0,0)
        
        self.WidthMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode('WidthMax')).GetValue()
        self.HeightMaxAbsolute = PySpin.CIntegerPtr(self.nodemap.GetNode('HeightMax')).GetValue()

        """
        self.set_ROI(0,0,self.WidthMaxAbsolute,self.HeightMaxAbsolute)

        self.WidthMax = self.WidthMaxAbsolute
        self.HeightMax = self.HeightMaxAbsolute
        self.OffsetX = PySpin.CIntegerPtr(self.nodemap.GetNode('OffsetX')).GetValue()
        self.OffsetY = PySpin.CIntegerPtr(self.nodemap.GetNode('OffsetY')).GetValue()
        """

        # fix the above rows to use presets defined in the config file for the ROI size
        self.set_ROI(0,0,self.WidthMaxAbsolute,self.HeightMaxAbsolute)

        # Apply default ROI from config (centered on sensor) - ADD THIS HERE
        roi_width = CAMERA_CONFIG.ROI_WIDTH_DEFAULT
        roi_height = CAMERA_CONFIG.ROI_HEIGHT_DEFAULT
        offset_x = int((self.WidthMaxAbsolute - roi_width) / 2)
        offset_y = int((self.HeightMaxAbsolute - roi_height) / 2)
        offset_x = max(0, min(offset_x, self.WidthMaxAbsolute - roi_width))
        offset_y = max(0, min(offset_y, self.HeightMaxAbsolute - roi_height))
        self.set_ROI(offset_x, offset_y, roi_width, roi_height)

        self.WidthMax = self.WidthMaxAbsolute
        self.HeightMax = self.HeightMaxAbsolute
        self.OffsetX = PySpin.CIntegerPtr(self.nodemap.GetNode('OffsetX')).GetValue()
        self.OffsetY = PySpin.CIntegerPtr(self.nodemap.GetNode('OffsetY')).GetValue()

        # disable gamma
        PySpin.CBooleanPtr(self.nodemap.GetNode('GammaEnable')).SetValue(False)
        PySpin.CBooleanPtr(self.nodemap.GetNode('GammaEnable')).SetValue(False)
        
    def set_callback(self,function):
        self.new_image_callback_external = function

    def enable_callback(self):
        if self.callback_is_enabled == False:
            # stop streaming
            if self.is_streaming:
                was_streaming = True
                self.stop_streaming()
            else:
                was_streaming = False
            # enable callback
            try:
                self.camera.RegisterEventHandler(self.image_event_handler)
                self.callback_is_enabled = True
            except PySpin.SpinnakerException as ex:
                print('Error: %s' % ex)
            # resume streaming if it was on
            if was_streaming:
                self.start_streaming()
            self.callback_is_enabled = True
        else:
            pass

    def disable_callback(self):
        if self.callback_is_enabled == True:
            # stop streaming
            if self.is_streaming:
                was_streaming = True
                self.stop_streaming()
            else:
                was_streaming = False
            try:
                self.camera.UnregisterEventHandler(self.image_event_handler)
                self.callback_is_enabled = False
            except PySpin.SpinnakerException as ex:
                print('Error: %s' % ex)
            # resume streaming if it was on
            if was_streaming:
                self.start_streaming()
        else:
            pass

    def open_by_sn(self,sn, is_color=None):
        self.sn = sn
        self.open(is_color=is_color)

    def close(self):
        try:
            self.camera.DeInit()
            del self.camera
        except AttributeError:
            pass
        self.camera = None
        self.auto_gain_mode = None
        self.auto_exposure_mode = None
        self.auto_wb_mode = None
        self.auto_wb_profile = None
        self.device_info_dict = None
        self.is_color = None
        self.gamma_lut = None
        self.contrast_lut = None
        self.color_correction_param = None
        self.last_raw_image = None
        self.last_converted_image = None
        self.last_numpy_image = None

    def set_exposure_time(self,exposure_time): ## NOTE: Disables auto-exposure
        use_strobe = (self.trigger_mode == TriggerMode.HARDWARE) # true if using hardware trigger
        self.nodemap = self.camera.GetNodeMap()
        node_auto_exposure = PySpin.CEnumerationPtr(self.nodemap.GetNode('ExposureAuto'))
        node_auto_exposure_off = PySpin.CEnumEntryPtr(node_auto_exposure.GetEntryByName('Off'))
        if not PySpin.IsReadable(node_auto_exposure_off) or not PySpin.IsWritable(node_auto_exposure):
            print("Unable to set exposure manually (cannot disable auto exposure)")
            return
        
        if node_auto_exposure.GetIntValue() != node_auto_exposure_off.GetValue():
            self.auto_exposure_mode = PySpin.CEnumEntryPtr(node_auto_exposure.GetCurrentEntry()).GetValue()

        node_auto_exposure.SetIntValue(node_auto_exposure_off.GetValue())

        node_exposure_time = PySpin.CFloatPtr(self.nodemap.GetNode('ExposureTime'))
        if not PySpin.IsWritable(node_exposure_time):
            print("Unable to set exposure manually after disabling auto exposure")

        if use_strobe == False or self.is_global_shutter:
            self.exposure_time = exposure_time
            node_exposure_time.SetValue(exposure_time*1000.0)
        else:
            # set the camera exposure time such that the active exposure time (illumination on time) is the desired value
            self.exposure_time = exposure_time
            # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
            camera_exposure_time = self.exposure_delay_us + self.exposure_time*1000 + self._readout_time_us() + 500 # add an additional 500 us so that the illumination can fully turn off before rows start to end exposure
            node_exposure_time.SetValue(camera_exposure_time)
        self._written_us = node_exposure_time.GetValue() if PySpin.IsReadable(node_exposure_time) else None
        self._flush_exposure_lag()

    def _flush_exposure_lag(self):
        """The camera applies a new exposure one trigger late (see __init__). So that the NEXT real image has the exposure that was just set, one throw-away
        frame is triggered now, while the light is off (the acquisition switches it on only for the real image). It is exposed with the OLD exposure; costs
        its exposure + readout, only after a change. Not done in live view (callback on), not streaming, other trigger modes; hardware trigger only when `dummy_trigger` is set."""
        if not self.exposure_lag_fix or self._flushing or self._written_us is None or self._latched_us == self._written_us:
            return
        hardware = self.trigger_mode == TriggerMode.HARDWARE
        if not self.is_streaming or self.callback_is_enabled:
            return
        if self.trigger_mode != TriggerMode.SOFTWARE and not (hardware and self.dummy_trigger is not None):
            return
        old_ms = 12000.0 if self._latched_us is None else self._latched_us / 1000.0
        self._flushing = True
        try:
            if hardware:
                self.dummy_trigger()          # pulse from the microcontroller, illumination stays off (measured: hardware trigger has the same lag)
                self._latched_us = self._written_us
            else:
                self.send_trigger()
            try:
                raw = self.camera.GetNextImage(int(2000 + 1.5 * old_ms))
                raw.Release()
            except PySpin.SpinnakerException as ex:
                print('*** FLIR camera: throw-away frame after an exposure change not received (' + str(ex) + ')')
                self._latched_us = None
        finally:
            self._flushing = False

    def _readout_time_us(self):
        """Time from the first to the last row starting its exposure (rolling shutter)."""
        if self.readout_us is not None:
            return self.readout_us
        return self.row_period_us*self.pixel_size_byte*(self.row_numbers-1)

    def _update_readout_timing(self):
        """towbinlab: measure the rolling-shutter readout of the current ROI and bit depth on the camera: with a short
        exposure the maximum frame rate is limited by the readout, so readout <= 1 / max frame rate. Using 1 / max frame
        rate errs on the safe side (slightly longer than the readout: never a partly lit image in hardware-trigger mode).
        Falls back to the old constants if the camera does not offer the nodes. Call while not streaming."""
        try:
            nodemap = self.camera.GetNodeMap()
            node_exposure = PySpin.CFloatPtr(nodemap.GetNode('ExposureTime'))
            node_rate = PySpin.CFloatPtr(nodemap.GetNode('AcquisitionFrameRate'))
            if not (PySpin.IsReadable(node_exposure) and PySpin.IsWritable(node_exposure) and PySpin.IsReadable(node_rate)):
                raise ValueError('ExposureTime / AcquisitionFrameRate not available')
            old = node_exposure.GetValue()
            node_exposure.SetValue(max(node_exposure.GetMin(), 100.0)) # short exposure: the frame rate is limited by the readout
            max_rate = node_rate.GetMax()
            node_exposure.SetValue(old)
            if not max_rate or max_rate <= 0:
                raise ValueError('no maximum frame rate')
            self.readout_us = 1e6 / max_rate
        except Exception as e:
            print('FLIR: readout time could not be measured (' + str(e) + '); using the default constants')
            self.readout_us = None
        self.exposure_delay_us = self.exposure_delay_us_8bit*self.pixel_size_byte
        self.strobe_delay_us = int(self.exposure_delay_us + self._readout_time_us())
        print('FLIR: rolling-shutter readout ' + str(round(self._readout_time_us() / 1000, 2)) + ' ms, strobe delay ' + str(round(self.strobe_delay_us / 1000, 2)) + ' ms')
        return self.readout_us

    def update_camera_exposure_time(self):
        self.set_exposure_time(self.exposure_time)

    def set_analog_gain(self,analog_gain): ## NOTE: Disables auto-gain
        self.nodemap = self.camera.GetNodeMap()
    
        node_auto_gain = PySpin.CEnumerationPtr(self.nodemap.GetNode('GainAuto'))
        node_auto_gain_off = PySpin.CEnumEntryPtr(node_auto_gain.GetEntryByName('Off'))
        if not PySpin.IsReadable(node_auto_gain_off) or not PySpin.IsWritable(node_auto_gain):
            print("Unable to set gain manually (cannot disable auto gain)")
            return

        if node_auto_gain.GetIntValue() != node_auto_gain_off.GetValue():
            self.auto_gain_mode = PySpin.CEnumEntryPtr(node_auto_gain.GetCurrentEntry()).GetValue()

        node_auto_gain.SetIntValue(node_auto_gain_off.GetValue())
        
        node_gain = PySpin.CFloatPtr(self.nodemap.GetNode('Gain'))

        if not PySpin.IsWritable(node_gain):
            print("Unable to set gain manually after disabling auto gain")
            return

        self.analog_gain = analog_gain
        node_gain.SetValue(analog_gain)

    def get_awb_ratios(self): ## NOTE: Enables auto WB, defaults to continuous WB
        self.nodemap = self.camera.GetNodeMap()
        node_balance_white_auto = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceWhiteAuto"))
        #node_balance_white_auto_options = [PySpin.CEnumEntryPtr(entry).GetName() for entry in node_balance_white_auto.GetEntries()]
        #print("WB Auto options: "+str(node_balance_white_auto_options))

        node_balance_ratio_select = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceRatioSelector"))
        #node_balance_ratio_select_options = [PySpin.CEnumEntryPtr(entry).GetName() for entry in node_balance_ratio_select.GetEntries()]
        #print("Balance Ratio Select options: "+str(node_balance_ratio_select_options))
        """
        node_balance_profile = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceWhiteAutoProfile"))
        node_balance_profile_options= [PySpin.CEnumEntryPtr(entry).GetName() for entry in node_balance_profile.GetEntries()]
        print("WB Auto Profile options: "+str(node_balance_profile_options))
        """
        node_balance_white_auto_off = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntryByName('Off'))
        if not PySpin.IsReadable(node_balance_white_auto) or not PySpin.IsReadable(node_balance_white_auto_off):
            print("Unable to check if white balance is auto or not")

        elif PySpin.IsWritable(node_balance_white_auto) and node_balance_white_auto.GetIntValue() == node_balance_white_auto_off.GetValue():
            if self.auto_wb_mode is not None:
                node_balance_white_auto.SetIntValue(self.auto_wb_mode)
            else:
                node_balance_white_continuous = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntryByName('Continuous'))
                if PySpin.IsReadable(node_balance_white_continuous):
                    node_balance_white_auto.SetIntValue(node_balance_white_continuous.GetValue())
                else:
                    print("Cannot turn on auto white balance in continuous mode")
                    node_balance_white_once = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntry('Once'))
                    if PySpin.IsReadable(node_balance_white_once):
                        node_balance_white_auto.SetIntValue(node_balance_white_once.GetValue())
                    else:
                        print("Cannot turn on auto white balance in Once mode")
        else:
            print("Cannot turn on auto white balance, or auto white balance is already on")

        balance_ratio_red = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Red"))
        balance_ratio_green = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Green"))
        balance_ratio_blue = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Blue"))
        node_balance_ratio = PySpin.CFloatPtr(self.nodemap.GetNode("BalanceRatio"))
        if not PySpin.IsWritable(node_balance_ratio_select) or not PySpin.IsReadable(balance_ratio_red) or not PySpin.IsReadable(balance_ratio_green) or not PySpin.IsReadable(balance_ratio_blue):
            print("Unable to move balance ratio selector")
            return (0,0,0)

        node_balance_ratio_select.SetIntValue(balance_ratio_red.GetValue())
        if not PySpin.IsReadable(node_balance_ratio):
            print("Unable to read balance ratio for red")
            awb_r = 0
        else:
            awb_r = node_balance_ratio.GetValue()

        node_balance_ratio_select.SetIntValue(balance_ratio_green.GetValue())
        if not PySpin.IsReadable(node_balance_ratio):
            print("Unable to read balance ratio for green")
            awb_g = 0
        else:
            awb_g = node_balance_ratio.GetValue()

        node_balance_ratio_select.SetIntValue(balance_ratio_blue.GetValue())
        if not PySpin.IsReadable(node_balance_ratio):
            print("Unable to read balance ratio for blue")
            awb_b = 0
        else:
            awb_b = node_balance_ratio.GetValue()

        return (awb_r, awb_g, awb_b)

    def set_wb_ratios(self, wb_r=None, wb_g=None, wb_b=None): ## NOTE disables auto WB, stores extant
                                                              ## auto WB mode if any
        self.nodemap = self.camera.GetNodeMap()
        node_balance_white_auto = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceWhiteAuto"))
        node_balance_ratio_select = PySpin.CEnumerationPtr(self.nodemap.GetNode("BalanceRatioSelector"))
        node_balance_white_auto_off = PySpin.CEnumEntryPtr(node_balance_white_auto.GetEntryByName('Off'))
        if not PySpin.IsReadable(node_balance_white_auto) or not PySpin.IsReadable(node_balance_white_auto_off):
            print("Unable to check if white balance is auto or not")
        elif node_balance_white_auto.GetIntValue() != node_balance_white_auto_off.GetValue():
            self.auto_wb_value = node_balance_white_auto.GetIntValue()
            if PySpin.IsWritable(node_balance_white_auto):
                node_balance_white_auto.SetIntValue(node_balance_white_auto_off.GetValue())
            else:
                print("Cannot turn off auto WB")
        
        balance_ratio_red = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Red"))
        balance_ratio_green = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Green"))
        balance_ratio_blue = PySpin.CEnumEntryPtr(node_balance_ratio_select.GetEntryByName("Blue"))
        node_balance_ratio = PySpin.CFloatPtr(self.nodemap.GetNode("BalanceRatio"))
        if not PySpin.IsWritable(node_balance_ratio_select) or not PySpin.IsReadable(balance_ratio_red) or not PySpin.IsReadable(balance_ratio_green) or not PySpin.IsReadable(balance_ratio_blue):
            print("Unable to move balance ratio selector")
            return

        node_balance_ratio_select.SetIntValue(balance_ratio_red.GetValue())
        if not PySpin.IsWritable(node_balance_ratio):
            print("Unable to write balance ratio for red")
        else:
            if wb_r is not None:
                node_balance_ratio.SetValue(wb_r)

        node_balance_ratio_select.SetIntValue(balance_ratio_green.GetValue())
        if not PySpin.IsWritable(node_balance_ratio):
            print("Unable to write balance ratio for green")
        else:
            if wb_g is not None:
                node_balance_ratio.SetValue(wb_g)

        node_balance_ratio_select.SetIntValue(balance_ratio_blue.GetValue())
        if not PySpin.IsWritable(node_balance_ratio):
            print("Unable to write balance ratio for blue")
        else:
            if wb_b is not None:
                node_balance_ratio.SetValue(wb_b)

    def set_reverse_x(self,value):
        self.nodemap = self.camera.GetNodeMap()
        node_reverse_x = PySpin.CBooleanPtr(self.nodemap.GetNode('ReverseX'))
        if not PySpin.IsWritable(node_reverse_x):
            print("Can't write to reverse X node")
            return
        else:
            node_reverse_x.SetValue(bool(value))

    def set_reverse_y(self,value):
        self.nodemap = self.camera.GetNodeMap()
        node_reverse_y = PySpin.CBooleanPtr(self.nodemap.GetNode('ReverseY'))
        if not PySpin.IsWritable(node_reverse_y):
            print("Can't write to reverse Y node")
            return
        else:
            node_reverse_y.SetValue(bool(value))

    def start_streaming(self):
        self.camera.Init()

        if not self.is_streaming:
            try:
                self.camera.BeginAcquisition()
            except PySpin.SpinnakerException as ex:
                print("Spinnaker exception: "+str(ex))
        if self.camera.IsStreaming():
            print("Camera is streaming")
            self.is_streaming = True

    def stop_streaming(self):
        if self.is_streaming:
            try:
                self.camera.EndAcquisition()
            except PySpin.SpinnakerException as ex:
                print("Spinnaker exception: "+str(ex))
        if not self.camera.IsStreaming():
            print("Camera is not streaming")
            self.is_streaming = False

    def set_pixel_format(self,pixel_format,convert_if_not_native=False):
        if self.is_streaming == True:
            was_streaming = True
            self.stop_streaming()
        else:
            was_streaming = False
        self.nodemap = self.camera.GetNodeMap()
        
        node_pixel_format = PySpin.CEnumerationPtr(self.nodemap.GetNode('PixelFormat'))
        node_adc_bit_depth = PySpin.CEnumerationPtr(self.nodemap.GetNode('AdcBitDepth'))

        if PySpin.IsWritable(node_pixel_format) and PySpin.IsWritable(node_adc_bit_depth):
            pixel_selection =  None
            pixel_size_byte = None
            adc_bit_depth = None
            fallback_pixel_selection = None
            conversion_pixel_format = None
            if pixel_format == 'MONO8':
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono8'))
                conversion_pixel_format = PySpin.PixelFormat_Mono8
                pixel_size_byte = 1
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit10'))
            if pixel_format == 'MONO10': 
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono10'))
                fallback_pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono10p'))
                conversion_pixel_format = PySpin.PixelFormat_Mono8
                pixel_size_byte = 1
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit10'))
            if pixel_format == 'MONO12':
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono12'))
                fallback_pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono12p'))
                conversion_pixel_format = PySpin.PixelFormat_Mono16
                pixel_size_byte = 2
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit12'))
            if pixel_format == 'MONO14': # MONO14/16 are aliases of each other, since they both
                                         # do ADC at bit depth 14
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono16'))
                conversion_pixel_format = PySpin.PixelFormat_Mono16
                pixel_size_byte = 2
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit14'))
            if pixel_format == 'MONO16':
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('Mono16'))
                conversion_pixel_format = PySpin.PixelFormat_Mono16
                pixel_size_byte = 2
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit14'))
            if pixel_format == 'BAYER_RG8':
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('BayerRG8'))
                conversion_pixel_format = PySpin.PixelFormat_BayerRG8
                pixel_size_byte = 1
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit10'))
            if pixel_format == 'BAYER_RG12':
                pixel_selection = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName('BayerRG12'))
                conversion_pixel_format = PySpin.PixelFormat_BayerRG12
                pixel_size_byte = 2
                adc_bit_depth = PySpin.CEnumEntryPtr(node_adc_bit_depth.GetEntryByName('Bit12'))

            if pixel_selection is not None and adc_bit_depth is not None:
                if PySpin.IsReadable(pixel_selection):
                    node_pixel_format.SetIntValue(pixel_selection.GetValue())
                    self.pixel_size_byte = pixel_size_byte
                    self.pixel_format = pixel_format
                    self.convert_pixel_format = False
                    if PySpin.IsReadable(adc_bit_depth):
                        node_adc_bit_depth.SetIntValue(adc_bit_depth.GetValue())
                elif PySpin.IsReadable(fallback_pixel_selection):
                    node_pixel_format.SetIntValue(fallback_pixel_selection.GetValue())
                    self.pixel_size_byte = pixel_size_byte
                    self.pixel_format = pixel_format
                    self.conversion_pixel_format = conversion_pixel_format
                    self.convert_pixel_format = True
                    if PySpin.IsReadable(adc_bit_depth):
                        node_adc_bit_depth.SetIntValue(adc_bit_depth.GetValue())
                else:
                    self.convert_pixel_format = convert_if_not_native
                    if convert_if_not_native:
                        self.conversion_pixel_format = conversion_pixel_format
                    print("Pixel format not available for this camera")
                    if PySpin.IsReadable(adc_bit_depth):
                        node_adc_bit_depth.SetIntValue(adc_bit_depth.GetValue())
                        print("Still able to set ADC bit depth to "+adc_bit_depth.GetSymbolic())

            else:
                print("Pixel format not implemented for Squid")

        else:
            print("pixel format is not writable")

        # update the exposure delay and strobe delay (towbinlab: readout measured for the new bit depth)
        self._update_readout_timing()
        self.update_camera_exposure_time()

        if was_streaming:
           self.start_streaming()

    def set_continuous_acquisition(self):
        self.nodemap = self.camera.GetNodeMap()
        node_trigger_mode = PySpin.CEnumerationPtr(self.nodemap.GetNode('TriggerMode'))
        node_trigger_mode_off = PySpin.CEnumEntryPtr(node_trigger_mode.GetEntryByName('Off'))
        if not PySpin.IsWritable(node_trigger_mode) or not PySpin.IsReadable(node_trigger_mode_off):
            print("Cannot toggle TriggerMode")
            return
        node_trigger_mode.SetIntValue(node_trigger_mode_off.GetValue())
        self.trigger_mode = TriggerMode.CONTINUOUS
        self._latched_us = None      # free-running frames use their own exposure
        self.update_camera_exposure_time()

    def set_triggered_acquisition_flir(self, source, activation=None):
        self.nodemap = self.camera.GetNodeMap()
        node_trigger_mode = PySpin.CEnumerationPtr(self.nodemap.GetNode('TriggerMode'))
        node_trigger_mode_on = PySpin.CEnumEntryPtr(node_trigger_mode.GetEntryByName('On'))
        if not PySpin.IsWritable(node_trigger_mode) or not PySpin.IsReadable(node_trigger_mode_on):
            print("Cannot toggle TriggerMode")
            return
        node_trigger_source = PySpin.CEnumerationPtr(self.nodemap.GetNode('TriggerSource'))
        node_trigger_source_option = PySpin.CEnumEntryPtr(node_trigger_source.GetEntryByName(str(source)))

        node_trigger_mode.SetIntValue(node_trigger_mode_on.GetValue())
        self._latched_us = None      # unknown after a change of the trigger mode

        if not PySpin.IsWritable(node_trigger_source) or not PySpin.IsReadable(node_trigger_source_option):
            print("Cannot set Trigger source")
            return

        node_trigger_source.SetIntValue(node_trigger_source_option.GetValue())

        if source != "Software" and activation is not None: # Set activation criteria for hardware trigger
            node_trigger_activation = PySpin.CEnumerationPtr(self.nodemap.GetNode('TriggerActivation'))
            node_trigger_activation_option = PySpin.CEnumEntryPtr(node_trigger_activation.GetEntryByName(str(activation)))
            if not PySpin.IsWritable(node_trigger_activation) or not PySpin.IsReadable(node_trigger_activation_option): 
                print("Cannot set trigger activation mode")
                return
            node_trigger_activation.SetIntValue(node_trigger_activation_option.GetValue())

    def set_software_triggered_acquisition(self):

        self.set_triggered_acquisition_flir(source='Software')

        self.trigger_mode = TriggerMode.SOFTWARE
        self.update_camera_exposure_time()

    def set_hardware_triggered_acquisition(self, source='Line2', activation='RisingEdge'):
        self.set_triggered_acquisition_flir(source=source, activation=activation)
        self.frame_ID_offset_hardware_trigger = None
        self.trigger_mode = TriggerMode.HARDWARE
        self.update_camera_exposure_time()

    def send_trigger(self):
        if self.is_streaming:
            self.nodemap = self.camera.GetNodeMap()
            node_trigger = PySpin.CCommandPtr(self.nodemap.GetNode('TriggerSoftware'))
            if not PySpin.IsWritable(node_trigger):
                print('Trigger node not writable')
                return
            node_trigger.Execute()
            self._latched_us = self._written_us
        else:
        	print('trigger not sent - camera is not streaming')

    MAX_FAILED_READS = 3 # stability: after this many failed reads in a row read_frame raises (the acquisition stops with a message)

    def _read_failed(self, reason):
        """stability: a failed read returns None (the acquisition skips that image and switches the light off) instead
        of a black np.zeros((Width, Height)) image that was saved as data; several failures in a row raise."""
        self._failed_reads = getattr(self, '_failed_reads', 0) + 1
        print('*** FLIR camera: no image (' + reason + '), failure ' + str(self._failed_reads) + ' in a row')
        if self._failed_reads >= self.MAX_FAILED_READS:
            self._failed_reads = 0
            raise RuntimeError('FLIR camera delivered no image ' + str(self.MAX_FAILED_READS) + ' times in a row (last: ' + reason + ')')
        return None

    def read_frame(self):
        if not self.camera.IsStreaming():
            print("Cannot read frame, camera not streaming")
            return self._read_failed('camera not streaming')
        callback_was_enabled = False
        if self.callback_is_enabled: # need to disable callback to read stream manually
            callback_was_enabled = True
            self.disable_callback()
        # stability: the timeout was a fixed 1000 ms, so exposures of about 1 s or more always timed out
        # (in hardware trigger mode the camera exposure also includes the rolling-shutter readout)
        timeout_ms = int(1000 + 1.5*float(getattr(self, 'exposure_time', 0) or 0))
        try:
            raw_image = self.camera.GetNextImage(timeout_ms)
        except PySpin.SpinnakerException as ex:
            if callback_was_enabled:
                self.enable_callback()
            return self._read_failed('GetNextImage(' + str(timeout_ms) + ' ms): ' + str(ex))
        if raw_image.IsIncomplete():
            print('Image incomplete with image status %d ...' % raw_image.GetImageStatus())
            raw_image.Release()
            if callback_was_enabled:
                self.enable_callback()
            return self._read_failed('incomplete image')
        self._failed_reads = 0

        if self.is_color and 'mono' not in self.pixel_format.lower():
            if "10" in self.pixel_format or "12" in self.pixel_format or "14" in self.pixel_format or "16" in self.pixel_format:
                rgb_image = self.one_frame_post_processor.Convert(raw_image,PySpin.PixelFormat_RGB16)
            else:
                rgb_image = self.one_frame_post_processor.Convert(raw_image,PySpin.PixelFormat_RGB8)
            numpy_image = rgb_image.GetNDArray()
            if self.pixel_format == 'BAYER_RG12':
                numpy_image = numpy_image << 4
        else:
            if self.convert_pixel_format:
                converted_image = self.one_frame_post_processor.Convert(raw_image,self.conversion_pixel_format)
                numpy_image = converted_image.GetNDArray()
                if self.conversion_pixel_format == PySpin.PixelFormat_Mono12:
                    numpy_image = numpy_image << 4
            else:
                try:
                    numpy_image = raw_image.GetNDArray()
                except PySpin.SpinnakerException:
                    print("Encountered problem getting ndarray, falling back to conversion to Mono8")
                    converted_image = self.one_frame_post_processor.Convert(raw_image, PySpin.PixelFormat_Mono8)
                    numpy_image = converted_image.GetNDArray()
                if self.pixel_format == 'MONO12':
                    numpy_image = numpy_image <<4
        # self.current_frame = numpy_image
        raw_image.Release()
        if callback_was_enabled: # reenable callback if it was disabled
            self.enable_callback()
        return numpy_image
    
    # ------------------------------------------------------------------------------------------------
    # towbinlab: fast acquisition (movies at video rate)
    # ------------------------------------------------------------------------------------------------
    def _snapshot_state(self):
        """Everything fast_acquire changes, so that it can be put back exactly (also after an error)."""
        nm = self.camera.GetNodeMap()
        st = dict(streaming=bool(self.is_streaming), callback=bool(self.callback_is_enabled), trigger_mode=self.trigger_mode,
                  pixel_format=self.pixel_format, exposure_time=self.exposure_time, analog_gain=self.analog_gain)

        def val(name, kind):
            n = kind(nm.GetNode(name))
            return n.GetValue() if (n is not None and PySpin.IsReadable(n)) else None
        st['roi'] = tuple(val(k, PySpin.CIntegerPtr) for k in ('OffsetX', 'OffsetY', 'Width', 'Height'))
        st['rate'] = val('AcquisitionFrameRate', PySpin.CFloatPtr)
        st['rate_enable'] = val('AcquisitionFrameRateEnable', PySpin.CBooleanPtr)
        st['throughput'] = val('DeviceLinkThroughputLimit', PySpin.CIntegerPtr)
        st['stream'] = {}
        try:
            tl = self.camera.GetTLStreamNodeMap()
            n = PySpin.CEnumerationPtr(tl.GetNode('StreamBufferCountMode'))
            if n is not None and PySpin.IsReadable(n):
                st['stream']['StreamBufferCountMode'] = n.GetIntValue()
            n = PySpin.CIntegerPtr(tl.GetNode('StreamBufferCountManual'))
            if n is not None and PySpin.IsReadable(n):
                st['stream']['StreamBufferCountManual'] = n.GetValue()
        except Exception:
            pass
        return st

    def _set_stream_buffers(self, count):
        """Enlarge the Spinnaker stream buffer (frames waiting for the program) so that short stalls of the host do not lose frames. -> count used or None"""
        try:
            tl = self.camera.GetTLStreamNodeMap()
            mode = PySpin.CEnumerationPtr(tl.GetNode('StreamBufferCountMode'))
            manual = PySpin.CIntegerPtr(tl.GetNode('StreamBufferCountManual'))
            if not (PySpin.IsWritable(mode) and PySpin.IsWritable(manual)):
                return None
            entry = mode.GetEntryByName('Manual')
            if entry is None or not PySpin.IsReadable(entry):
                return None
            mode.SetIntValue(entry.GetValue())
            n = int(min(max(count, manual.GetMin()), manual.GetMax()))
            manual.SetValue(n)
            return n
        except PySpin.SpinnakerException as ex:
            print('FLIR fast acquisition: stream buffers not set (' + str(ex) + ')')
            return None

    def _restore_state(self, st):
        """Put the camera back as it was before fast_acquire. Every step is tried even if an earlier one fails. -> list of problems (empty = ok)"""
        problems = []

        def step(what, fn):
            try:
                fn()
            except Exception as ex:             # keep restoring
                problems.append(what + ': ' + str(ex))
        nm = self.camera.GetNodeMap()
        try:
            if self.camera.IsStreaming():
                self.stop_streaming()
        except Exception as ex:
            problems.append('stop streaming: ' + str(ex))
        try:
            tl = self.camera.GetTLStreamNodeMap()
            for name, v in st['stream'].items():
                if name == 'StreamBufferCountMode':
                    step(name, lambda v=v: PySpin.CEnumerationPtr(tl.GetNode(name)).SetIntValue(v))
                else:
                    step(name, lambda v=v: PySpin.CIntegerPtr(tl.GetNode(name)).SetValue(v))
        except Exception as ex:
            problems.append('stream buffers: ' + str(ex))
        # ROI: offsets to 0 first so that the full size fits, then size, then the offsets
        ox, oy, w, h = st['roi']
        if None not in st['roi']:
            step('ROI', lambda: (self.set_ROI(offset_x=0, offset_y=0), self.set_ROI(width=w, height=h), self.set_ROI(offset_x=ox, offset_y=oy)))
        if st['pixel_format'] and st['pixel_format'] != self.pixel_format:
            step('pixel format', lambda: self.set_pixel_format(st['pixel_format']))
        self.exposure_time = st['exposure_time']
        mode = st['trigger_mode']
        if mode == TriggerMode.HARDWARE:
            step('trigger mode', self.set_hardware_triggered_acquisition)
        elif mode == TriggerMode.SOFTWARE:
            step('trigger mode', self.set_software_triggered_acquisition)
        elif mode == TriggerMode.CONTINUOUS:
            step('trigger mode', self.set_continuous_acquisition)
        step('exposure', self.update_camera_exposure_time)
        step('gain', lambda: self.set_analog_gain(st['analog_gain']))
        if st['rate'] is not None:
            step('frame rate', lambda: PySpin.CFloatPtr(nm.GetNode('AcquisitionFrameRate')).SetValue(
                min(st['rate'], PySpin.CFloatPtr(nm.GetNode('AcquisitionFrameRate')).GetMax())))
        if st['rate_enable'] is not None:
            step('frame rate enable', lambda: PySpin.CBooleanPtr(nm.GetNode('AcquisitionFrameRateEnable')).SetValue(st['rate_enable']))
        if st['callback']:
            step('callback', self.enable_callback)
        if st['streaming']:
            step('start streaming', self.start_streaming)
        for p in problems:
            print('*** FLIR fast acquisition: could not restore ' + p)
        return problems

    def fast_acquire(self, n_frames, fps, bits=None, roi=None, exposure_ms=None, gain=None, should_abort=None, on_frame=None,
                     max_bytes=4 * 1024 ** 3, buffers=64, on_started=None, on_stopped=None):
        """Record `n_frames` back to back at `fps` with the camera free running, into one preallocated array (no disk access, no processing in the loop).

        bits: 8 or 16 (None = keep the current pixel format); roi: side of a square or (width, height) in pixels, centred on the sensor (None = keep);
        exposure_ms / gain: None = keep. The illumination has to be on already (the caller switches it). EVERYTHING that is changed (pixel format, ROI,
        trigger mode, exposure, gain, frame rate, stream buffers, streaming, callback) is put back afterwards, also after an error or abort.
        on_started(info) is called when streaming has started (info = what was set), on_frame(i, image) after every frame (keep it short),
        on_stopped() right after the last frame (before the camera is put back), e.g. to switch the illumination off.
        Returns dict: frames (n, h, w) of the frames received, frame_ids, timestamps_ns (camera clock), incomplete, timeouts, aborted, error,
        fps_requested, fps_set, exposure_ms, gain, pixel_format, roi (x, y, w, h), buffers, restore_problems."""
        if self.is_color:
            raise ValueError('fast acquisition supports monochrome cameras only')
        n_frames = int(n_frames)
        if n_frames < 1 or not fps or fps <= 0:
            raise ValueError('fast_acquire: n_frames must be >= 1 and fps > 0')
        fmt = None if bits is None else {8: 'MONO8', 16: 'MONO16'}.get(int(bits))
        if bits is not None and fmt is None:
            raise ValueError('fast_acquire: bits must be 8 or 16')
        out = dict(frames=None, frame_ids=[], timestamps_ns=[], incomplete=0, timeouts=0, aborted=False, error=None, fps_requested=float(fps),
                   fps_set=None, exposure_ms=None, gain=None, pixel_format=None, roi=None, buffers=None, restore_problems=[])
        st = self._snapshot_state()
        try:
            if self.is_streaming:
                self.stop_streaming()
            if self.callback_is_enabled:
                self.disable_callback()
            nm = self.camera.GetNodeMap()
            # sensor region, centred
            if roi is not None:
                w_req, h_req = (roi, roi) if np.isscalar(roi) else (roi[0], roi[1])
                wmax = PySpin.CIntegerPtr(nm.GetNode('WidthMax')).GetValue()
                hmax = PySpin.CIntegerPtr(nm.GetNode('HeightMax')).GetValue()
                w_req, h_req = min(int(w_req), wmax), min(int(h_req), hmax)
                self.set_ROI(offset_x=0, offset_y=0)
                self.set_ROI(width=w_req, height=h_req)
                w_got = PySpin.CIntegerPtr(nm.GetNode('Width')).GetValue()
                h_got = PySpin.CIntegerPtr(nm.GetNode('Height')).GetValue()
                self.set_ROI(offset_x=(wmax - w_got) // 2, offset_y=(hmax - h_got) // 2)
            if fmt is not None and fmt != self.pixel_format:
                self.set_pixel_format(fmt)
            self.set_continuous_acquisition()              # free running; also re-applies the exposure without the strobe margin
            if exposure_ms is not None:
                self.set_exposure_time(float(exposure_ms))
            if gain is not None:
                self.set_analog_gain(float(gain))
            out['exposure_ms'], out['gain'] = self.exposure_time, self.analog_gain
            roi_now = tuple(PySpin.CIntegerPtr(nm.GetNode(k)).GetValue() for k in ('OffsetX', 'OffsetY', 'Width', 'Height'))
            out['roi'] = (roi_now[0], roi_now[1], roi_now[2], roi_now[3])
            h, w = roi_now[3], roi_now[2]
            out['pixel_format'] = self.pixel_format
            nbytes = n_frames * h * w * max(1, int(self.pixel_size_byte))
            if nbytes > max_bytes:
                raise ValueError(f'fast_acquire: {n_frames} frames of {w}x{h} need {nbytes / 1e9:.1f} GB of memory, more than the allowed {max_bytes / 1e9:.1f} GB')
            # frame rate: the exposure has to fit into the frame period
            exp_us = PySpin.CFloatPtr(nm.GetNode('ExposureTime')).GetValue()
            if exp_us > 1e6 / fps:
                raise ValueError(f'fast_acquire: exposure {exp_us / 1000:.1f} ms is longer than the frame period {1000 / fps:.1f} ms at {fps:g} fps')
            en = PySpin.CBooleanPtr(nm.GetNode('AcquisitionFrameRateEnable'))
            if PySpin.IsWritable(en):
                en.SetValue(True)
            rate = PySpin.CFloatPtr(nm.GetNode('AcquisitionFrameRate'))
            if PySpin.IsWritable(rate):
                rate.SetValue(float(min(max(fps, rate.GetMin()), rate.GetMax())))
                out['fps_set'] = rate.GetValue()
            thr = PySpin.CIntegerPtr(nm.GetNode('DeviceLinkThroughputLimit'))
            if PySpin.IsWritable(thr):
                thr.SetValue(thr.GetMax())
            out['buffers'] = self._set_stream_buffers(buffers)
            self.start_streaming()
            if on_started is not None:
                on_started(dict(out))
            buf, n = None, 0
            try:
                timeout_ms = int(2000 + 1.5 * exp_us / 1000 + 2000.0 / fps)
                failed_in_row = 0
                while n < n_frames:
                    if should_abort is not None and should_abort():
                        out['aborted'] = True
                        break
                    try:
                        raw = self.camera.GetNextImage(timeout_ms)
                    except PySpin.SpinnakerException as ex:
                        out['timeouts'] += 1
                        failed_in_row += 1
                        print('*** FLIR fast acquisition: no image within ' + str(timeout_ms) + ' ms (' + str(ex) + ')')
                        if failed_in_row >= 3:
                            out['error'] = 'camera delivered no image 3 times in a row: ' + str(ex)
                            break
                        continue
                    try:
                        if raw.IsIncomplete():
                            out['incomplete'] += 1
                            print('Image incomplete with image status %d ...' % raw.GetImageStatus())
                            failed_in_row += 1
                            if failed_in_row >= 3 and n == 0:
                                out['error'] = 'only incomplete images received'
                                break
                            continue
                        failed_in_row = 0
                        arr = raw.GetNDArray()
                        if self.pixel_format == 'MONO12':
                            arr = arr << 4
                        if buf is None:
                            buf = np.empty((n_frames,) + arr.shape, dtype=arr.dtype)
                        buf[n] = arr
                        out['frame_ids'].append(int(raw.GetFrameID()))
                        try:
                            out['timestamps_ns'].append(int(raw.GetTimeStamp()))
                        except Exception:
                            out['timestamps_ns'].append(0)
                    finally:
                        raw.Release()
                    if on_frame is not None:
                        on_frame(n, buf[n])
                    n += 1
                out['frames'] = buf[:n] if buf is not None else None
            finally:
                if on_stopped is not None:
                    try:
                        on_stopped()
                    except Exception as ex:
                        print('*** FLIR fast acquisition: on_stopped failed: ' + str(ex))
        finally:
            out['restore_problems'] = self._restore_state(st)
            self._latched_us = None      # the free-running frames used their own exposure: the next real image needs a throw-away frame first
        return out

    def get_ROI(self):
        """(offset_x, offset_y, width, height) as the camera has them right now. Read-only: the values come from the camera's
        nodes, not from the cached self.Width / self.OffsetX (those are what was last requested, not what the camera accepted)."""
        nodemap = self.camera.GetNodeMap()
        return tuple(int(PySpin.CIntegerPtr(nodemap.GetNode(name)).GetValue()) for name in ('OffsetX', 'OffsetY', 'Width', 'Height'))

    def set_ROI(self,offset_x=None,offset_y=None,width=None,height=None):

        # stop streaming if streaming is on
        if self.is_streaming == True:
            was_streaming = True
            self.stop_streaming()
        else:
            was_streaming = False

        self.nodemap = self.camera.GetNodeMap()
        node_width = PySpin.CIntegerPtr(self.nodemap.GetNode('Width'))
        node_height = PySpin.CIntegerPtr(self.nodemap.GetNode('Height'))
        node_width_max = PySpin.CIntegerPtr(self.nodemap.GetNode('WidthMax'))
        node_height_max = PySpin.CIntegerPtr(self.nodemap.GetNode('HeightMax'))
        node_offset_x = PySpin.CIntegerPtr(self.nodemap.GetNode('OffsetX'))
        node_offset_y = PySpin.CIntegerPtr(self.nodemap.GetNode('OffsetY'))

        if width is not None:
            # update the camera setting
            if PySpin.IsWritable(node_width):
                node_min = node_width.GetMin()
                node_inc = node_width.GetInc()
                diff = width-node_min
                num_incs = diff//node_inc
                width = node_min+num_incs*node_inc
                self.Width = width
                node_width.SetValue(min(max(int(width),0),node_width_max.GetValue()))
            else:
                print("Width is not implemented or not writable")

        if height is not None:
            # update the camera setting
            if PySpin.IsWritable(node_height):
                node_min = node_height.GetMin()
                node_inc = node_height.GetInc()
                diff = height-node_min
                num_incs = diff//node_inc
                height = node_min+num_incs*node_inc

                self.Height = height
                node_height.SetValue(min(max(int(height),0),node_height_max.GetValue()))
            else:
                print("Height is not implemented or not writable")

        if offset_x is not None:
            # update the camera setting
            if PySpin.IsWritable(node_offset_x):
                node_min = node_offset_x.GetMin()
                node_max = node_offset_x.GetMax()
                node_inc = node_offset_x.GetInc()
                diff = offset_x-node_min
                num_incs = diff//node_inc
                offset_x = node_min+num_incs*node_inc

                self.OffsetX = offset_x
                node_offset_x.SetValue(min(int(offset_x), node_max))
            else:
                print("OffsetX is not implemented or not writable")
        
        if offset_y is not None:
            # update the camera setting
            if PySpin.IsWritable(node_offset_y):
                node_min = node_offset_y.GetMin()
                node_max = node_offset_y.GetMax()
                node_inc = node_offset_y.GetInc()
                diff = offset_y-node_min
                num_incs = diff//node_inc
                offset_y = node_min+num_incs*node_inc

                self.OffsetY = offset_y
                node_offset_y.SetValue(min(int(offset_y), node_max))
            else:
                print("OffsetY is not implemented or not writable")

        
        # towbinlab: the readout depends on the number of rows
        if self.camera is not None and (width is not None or height is not None):
            self._update_readout_timing()
            self.update_camera_exposure_time()

        # restart streaming if it was previously on
        if was_streaming == True:
            self.start_streaming()

    def reset_camera_acquisition_counter(self):
        self.nodemap = self.camera.GetNodeMap()
        node_counter_event_source = PySpin.CEnumerationPtr(self.nodemap.GetNode('CounterEventSource'))
        node_counter_event_source_line2 = PySpin.CEnumEntryPtr(node_counter_event_source.GetEntryByName('Line2'))
        if PySpin.IsWritable(node_counter_event_source) and PySpin.IsReadable(node_counter_event_source_line2):
            node_counter_event_source.SetIntValue(node_counter_event_source_line2.GetValue())
        else:
            print("CounterEventSource is not implemented or not writable, or Line 2 is not an option")

        node_counter_reset = PySpin.CCommandPtr(self.nodemap.GetNode('CounterReset'))

        if PySpin.IsImplemented(node_counter_reset) and PySpin.IsWritable(node_counter_reset):
            node_counter_reset.Execute()
        else:
            print("CounterReset is not implemented")

    def set_line3_to_strobe(self): #FLIR cams don't have the right Line layout for this
        # self.camera.StrobeSwitch.set(gx.GxSwitchEntry.ON)
        #self.nodemap = self.camera.GetNodeMap()
        
        #node_line_selector = PySpin.CEnumerationPtr(self.nodemap.GetNode('LineSelector'))

        #node_line3 = PySpin.CEnumEntryPtr(node_line_selector.GetEntryByName('Line3'))
        
        #self.camera.LineSelector.set(gx.GxLineSelectorEntry.LINE3)
        #self.camera.LineMode.set(gx.GxLineModeEntry.OUTPUT)
        #self.camera.LineSource.set(gx.GxLineSourceEntry.STROBE)
        pass
    
    def set_line3_to_exposure_active(self): #BlackFly cam has no output on Line 3
        # self.camera.StrobeSwitch.set(gx.GxSwitchEntry.ON)
        #self.camera.LineSelector.set(gx.GxLineSelectorEntry.LINE3)
        #self.camera.LineMode.set(gx.GxLineModeEntry.OUTPUT)
        #self.camera.LineSource.set(gx.GxLineSourceEntry.EXPOSURE_ACTIVE)
        pass

    def __del__(self):
        try:
            self.stop_streaming()
            self.camera.DeInit()
            del self.camera
        except AttributeError:
            pass
        self.camera_list.Clear()
        self.py_spin_system.ReleaseInstance()

