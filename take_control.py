#!/usr/bin/env python3

import usb.core
import usb.util
from enum import Enum, IntEnum, unique

#
#USB code
#

@unique
class State(IntEnum):
  ENABLED = 1
  DISABLED = 0

@unique
class InputType(Enum):
  # Apogee Duet uses this value for each input type, that's why I choose these values 
  LINE_4DBU = 0
  LINE_10DBV = 1
  MICROPHONE = 2
  INSTRUMENT = 3
  
  def __str__(self):
    string_representations = {
      self.LINE_4DBU: '+4dBu',
      self.LINE_10DBV: '-10dBV',
      self.MICROPHONE: 'Microphone',
      self.INSTRUMENT: 'Instrument'
    }
    return string_representations[self]
    
  @classmethod
  def str_list(cls):
    return [str(t) for t in list(cls)]

class Input(object):
  # The Apogee bypasses the preamp for the two line types: the signal hits the
  # converters at a fixed reference level and neither the software nor the
  # front panel knob has any effect. Only the mic and instrument types expose a
  # gain register.
  _level_range = {
    InputType.MICROPHONE: { 
      'max': 75,
      'min': 0
    },
    InputType.INSTRUMENT: {
      'max': 65,
      'min': 0
    }
  }
  
  def __init__(self, device, index):
    self._device = device
    self.index = index
    self.number = index + 1
    self._type = device.get_input_type(self)
    self.phantom_power_state = State.DISABLED
    self._level = self._read_level()
    if self._type == InputType.MICROPHONE:
      self.phantom_power_state = device.get_phantom_power_state(self)
    self.phase_state = device.get_phase_state(self)
    self.softlimit_state = device.get_softlimit_state(self)
    self.group_state = device.get_group_state(self)

  def _read_level(self):
    if not self.has_level_control:
      return None
    return self._device.get_input_level(self)

  @property
  def has_level_control(self):
    return self._type in self._level_range

  @property
  def min_level(self):
    return self._level_range.get(self._type, {}).get('min')

  @property
  def max_level(self):
    return self._level_range.get(self._type, {}).get('max')
    
  @property
  def type_(self):
    return self._type    
    
  @type_.setter
  def type_(self, value):
    new_type = InputType(value)
    self._device.set_input_type(self, new_type)
    self._type = new_type
    # Changing the type changes the gain register and, when leaving microphone
    # mode, drops the input out of the (device wide) phantom power state.
    self._level = self._read_level()
    self.phantom_power_state = State.DISABLED
    if new_type == InputType.MICROPHONE:
      self.phantom_power_state = self._device.get_phantom_power_state(self)

  @property
  def level(self):
    return self._level

  @level.setter
  def level(self, value):
    if not self.has_level_control:
      return
    self._device.set_input_level(self, value)
    self._level = value
    
  def toggle_phantom_power(self):
    new_state = State(not self.phantom_power_state)
    self._device.set_phantom_power_state(self, new_state)
    self.phantom_power_state = new_state
    
  def toggle_phase(self):
    new_state = State(not self.phase_state)
    self._device.set_phase_state(self, new_state)
    self.phase_state = new_state
    
  def toggle_softlimit(self):
    new_state = State(not self.softlimit_state)
    self._device.set_softlimit_state(self, new_state)
    self.softlimit_state = new_state

  def toggle_group(self):
    new_state = State(not self.group_state)
    self._device.set_group_state(new_state)
    self.group_state = new_state

@unique 
class OutputSource(Enum):
  PLAYBACK_1_2 = 0
  PLAYBACK_3_4 = 1
  MIXER = 2

  def __str__(self):
    string_representations = {
      self.PLAYBACK_1_2: '1-2',
      self.PLAYBACK_3_4: '3-4',
      self.MIXER: 'Mixer'
    }
    return string_representations[self]

  @classmethod
  def str_list(cls):
    return [str(t) for t in list(cls)]

@unique
class SpeakerOutputType(Enum):
  # Apogee Duet uses this value for each input type, that's why I choose these values
  LINE_4DBU = 0
  LINE_10DBV = 1

  def __str__(self):
    string_representations = {
      self.LINE_4DBU: '+4dBu',
      self.LINE_10DBV: '-10dBV'
    }
    return string_representations[self]

  @classmethod
  def str_list(cls):
    return [str(t) for t in list(cls)]

@unique
class OutputType(Enum):
  # Apogee Duet uses this value for each input type, that's why I choose these values
  SPEAKERS = 0
  HEADPHONES = 1

  def __str__(self):
    string_representations = {
      self.SPEAKERS: 'Speakers',
      self.HEADPHONES: 'Headphones'
    }
    return string_representations[self]

class Output(object):
  _level_range = {
    'min': -64,
    'max': 0
  }
  min_level = _level_range['min']
  max_level = _level_range['max']
  
  def __init__(self, device, index):
    self._device = device
    self.index = index
    # Hard coded here, maybe in the future could be read from the device but I haven't been able to do it
    # I just used the "self.index" because speakers are already 0 and headphones are 1 from what I observed
    self.type_ = OutputType(self.index) 
    if self.type_ == OutputType.SPEAKERS:
      self._speaker_output_type = device.get_speaker_output_type(self)
    self.mute_state = device.get_mute_state(self)
    self.dim_state = device.get_dim_state(self)
    self.mono_state = device.get_mono_state(self)
    self._level = device.get_output_level(self)
    self._source = device.get_output_source(self)

  def toggle_mute(self):
    new_state = State(not self.mute_state)
    self._device.set_mute_state(self, new_state)
    self.mute_state = new_state

  def toggle_dim(self):
    new_state = State(not self.dim_state)
    self._device.set_dim_state(self, new_state)
    self.dim_state = new_state

  def toggle_mono(self):
    new_state = State(not self.mono_state)
    self._device.set_mono_state(self, new_state)
    self.mono_state = new_state

  @property
  def level(self):
    return self._level

  @level.setter
  def level(self, value):
    self._device.set_output_level(self, value)
    self._level = value

  @property
  def speaker_output_type(self):
    return self._speaker_output_type    

  @speaker_output_type.setter
  def speaker_output_type(self, value):
    new_type = SpeakerOutputType(value)
    self._device.set_speaker_output_type(self, new_type)
    self._speaker_output_type = new_type

  @property
  def source(self):
    return self._source    
    
  @source.setter
  def source(self, value):
    new_source = OutputSource(value)
    self._device.set_output_source(self, new_source)
    self._source = new_source


@unique 
class SoftwareReturnSource(Enum):
  PLAYBACK_1_2 = 0
  PLAYBACK_3_4 = 1

  def __str__(self):
    string_representations = {
      self.PLAYBACK_1_2: '1-2',
      self.PLAYBACK_3_4: '3-4',
    }
    return string_representations[self]

  @classmethod
  def str_list(cls):
    return [str(t) for t in list(cls)]

@unique
class ChannelType(Enum):
  INPUT = 0
  SOFTWARE_RETURN = 1
  MASTER = 2
  
  def __str__(self):
    string_representations = {
      self.INPUT: 'Input',
      self.SOFTWARE_RETURN: 'Software Return',
      self.MASTER: 'Master',
    }
    return string_representations[self]
    
class Channel(object):
  _level_range = {
    'min': -48,
    'max': 6,
  }
  _pan_range = {
    'min': -64,
    'max': 64
  }
  min_level = _level_range['min']
  max_level = _level_range['max']
  min_pan = _pan_range['min']
  max_pan = _pan_range['max']

  def __init__(self, device, index, type_):
    self._device = device
    self.index = index
    self.type_ = type_
    if not self.type_ == ChannelType.MASTER:
      self.mute_state = device.get_channel_mute_state(self)
      self.solo_state = device.get_channel_solo_state(self)
      if self.type_ == ChannelType.SOFTWARE_RETURN:
        self._source = device.get_software_return_source(self)

  @property
  def level(self):
    raw_value = self._device.get_channel_level(self)
    return raw_value + self.min_level

  @level.setter
  def level(self, value):
    value_to_device = value - self.min_level
    self._device.set_channel_level(self, value_to_device)

  @property
  def pan(self):
    raw_value = self._device.get_pan_value(self)
    return raw_value - self.max_pan

  @pan.setter
  def pan(self, value):
    value_to_device = value + self.max_pan
    self._device.set_pan_value(self, value_to_device)

  @property
  def source(self):
    return self._source    
    
  @source.setter
  def source(self, value):
    new_source = SoftwareReturnSource(value)
    self._device.set_software_return_source(self, new_source)
    self._source = new_source

  def toggle_mute(self):
    new_state = State(not self.mute_state)
    self._device.set_channel_mute_state(self, new_state)
    self.mute_state = new_state

  def toggle_solo(self):
    new_state = State(not self.solo_state)
    self._device.set_channel_solo_state(self, new_state)
    self.solo_state = new_state


class ApogeeDuet(object):
  idVendor = 0x0c60
  idProduct = 0x0016
  _WRITE = 0x40
  _READ = 0xc0
  _MIXER_CHANNEL_REQUESTS = {
    'SOFTWARE_RETURN_SOURCE': 54,
    'LEVEL': 76,
    'PAN': 77,
    'SOLO': 78,
    'MUTE': 79,
  }
  _OUTPUT_REQUESTS = {
    'LEVEL': 51,
    'MUTE': 53,
    'DIM': 64,
    'SUM_TO_MONO': 70,
    'SOURCE': 83,
    'SPEAKER_OUTPUT_TYPE': 182,
  }
  _INPUT_REQUESTS = {
    'SOFTLIMIT': 17,
    'PHASE': 19,
    'PHANTOM_POWER': 21,
    'TYPE': 22,
    'LEVEL': {
      InputType.MICROPHONE: 52, 
      InputType.INSTRUMENT: 62
    },
    'GROUP': 68,
  }
  
  def __init__(self):
    self._dev = usb.core.find(idVendor=self.idVendor, idProduct=self.idProduct)
    if self._dev is None:
      raise ValueError('Apogee Duet not found')
    # Hardcoded because I haven't implemented how to read inputs and outputs information from interface
    self.inputs = [
      Input(device=self, index=0), # Input 1
      Input(device=self, index=1)  # Input 2
    ]
    self.outputs = [
      Output(device=self, index=0), # Speakers
      Output(device=self, index=1)  # Headphones
    ]
    self.mixer_channels = [
      Channel(device=self, index=0, type_=ChannelType.INPUT),  # Input 1
      Channel(device=self, index=1, type_=ChannelType.INPUT),  # Input 2
      Channel(device=self, index=2, type_=ChannelType.SOFTWARE_RETURN),  # Software Return
      Channel(device=self, index=3, type_=ChannelType.MASTER)   # Mixer Master
    ]

  # The descriptive strings live in USB string descriptors, which some devices
  # refuse to hand out; the panel can live without them.
  @property
  def product_name(self):
    try:
      return self._dev.product
    except usb.core.USBError:
      return None

  @property
  def serial_number(self):
    try:
      return self._dev.serial_number
    except usb.core.USBError:
      return None
  
  # The Duet drops roughly one control transfer in a thousand with a USB I/O
  # error. Every register this driver touches holds an absolute value rather
  # than a delta, so re-sending a failed transfer cannot double apply it and
  # keeps a stray error from surfacing in the middle of a fader drag.
  _TRANSFER_ATTEMPTS = 2

  def _transfer(self, bmRequestType, bmRequest, wValue, wIndex, payload):
    for attempt in range(self._TRANSFER_ATTEMPTS):
      try:
        return self._dev.ctrl_transfer(bmRequestType, bmRequest, wValue, wIndex, payload)
      except usb.core.USBError:
        if attempt == self._TRANSFER_ATTEMPTS - 1:
          raise

  # Every read USB control transfer with the Apogee seems to follow the same format, just one byte returned
  def _get_value_from_device(self, bmRquest=None, wIndex=None):
    assert bmRquest != None
    assert wIndex != None
    bmRequestType = self._READ
    wValue = 0
    bytes_to_read = 1
    ret = self._transfer(bmRequestType, bmRquest, wValue, wIndex, bytes_to_read)
    return ret[0]
    
  # The same here for every write USB control transfer
  def _set_value_on_device(self, bmRequest=None, wIndex=None, message=None):
    assert bmRequest != None
    assert wIndex != None
    assert message != None
    bmRequestType = self._WRITE
    wValue = 0
    message = [message]
    assert self._transfer(bmRequestType, bmRequest, wValue, wIndex, message)
  
  #
  # Mixer
  #

  def get_channel_mute_state(self, channel=None):
    assert channel != None
    value = self._get_value_from_device(self._MIXER_CHANNEL_REQUESTS['MUTE'], channel.index)
    return State(value)

  def set_channel_mute_state(self, channel=None, state=None):
    self._set_value_on_device(self._MIXER_CHANNEL_REQUESTS['MUTE'], channel.index, state.value)

  def get_channel_solo_state(self, channel=None):
    assert channel != None
    value = self._get_value_from_device(self._MIXER_CHANNEL_REQUESTS['SOLO'], channel.index)
    return State(value)

  def set_channel_solo_state(self, channel=None, state=None):
    self._set_value_on_device(self._MIXER_CHANNEL_REQUESTS['SOLO'], channel.index, state.value)

  def get_channel_level(self, channel=None):
    assert channel != None
    value = self._get_value_from_device(self._MIXER_CHANNEL_REQUESTS['LEVEL'], channel.index)
    return value

  def set_channel_level(self, channel=None, level=None):
    assert channel != None
    assert level != None
    self._set_value_on_device(self._MIXER_CHANNEL_REQUESTS['LEVEL'], channel.index, level)

  def get_pan_value(self, channel=None):
    assert channel != None
    value = self._get_value_from_device(self._MIXER_CHANNEL_REQUESTS['PAN'], channel.index)
    return value

  def set_pan_value(self, channel=None, value=None):
    assert channel != None
    assert value != None
    self._set_value_on_device(self._MIXER_CHANNEL_REQUESTS['PAN'], channel.index, value)

  def get_software_return_source(self, channel=None):
    assert channel != None
    value = self._get_value_from_device(self._MIXER_CHANNEL_REQUESTS['SOFTWARE_RETURN_SOURCE'], channel.index)
    return SoftwareReturnSource(value)

  def set_software_return_source(self, channel=None, source=None):
    self._set_value_on_device(self._MIXER_CHANNEL_REQUESTS['SOFTWARE_RETURN_SOURCE'], channel.index, source.value)

  #
  # Outputs
  #

  def get_output_source(self, output=None):
    assert output != None
    value = self._get_value_from_device(self._OUTPUT_REQUESTS['SOURCE'], output.index)
    return OutputSource(value)

  def set_output_source(self, output=None, source=None):
    self._set_value_on_device(self._OUTPUT_REQUESTS['SOURCE'], output.index, source.value)

  def get_output_level(self, output=None):
    assert output != None
    value = self._get_value_from_device(self._OUTPUT_REQUESTS['LEVEL'], output.index)
    return -value

  def set_output_level(self, output=None, level=None):
    assert output != None
    assert level != None
    self._set_value_on_device(self._OUTPUT_REQUESTS['LEVEL'], output.index, -level)

  def get_mute_state(self, output=None):
    assert output != None
    value = self._get_value_from_device(self._OUTPUT_REQUESTS['MUTE'], output.index)
    return State(value)

  def set_mute_state(self, output=None, state=None):
    self._set_value_on_device(self._OUTPUT_REQUESTS['MUTE'], output.index, state.value)

  def get_dim_state(self, output=None):
    assert output != None
    value = self._get_value_from_device(self._OUTPUT_REQUESTS['DIM'], output.index)
    return State(value)

  def set_dim_state(self, output=None, state=None):
    self._set_value_on_device(self._OUTPUT_REQUESTS['DIM'], output.index, state.value)

  def get_mono_state(self, output=None):
    assert output != None
    value = self._get_value_from_device(self._OUTPUT_REQUESTS['SUM_TO_MONO'], output.index)
    return State(value)

  def set_mono_state(self, output=None, state=None):
    self._set_value_on_device(self._OUTPUT_REQUESTS['SUM_TO_MONO'], output.index, state.value)

  def get_speaker_output_type(self, output=None):
    assert output != None
    # I'm using 0 which is the left channel I guess? 
    # because I'm assuming that the left and right channel have the same type selected. 
    # More details in set_speaker_output_type() 
    value = self._get_value_from_device(self._OUTPUT_REQUESTS['SPEAKER_OUTPUT_TYPE'], 0)
    return SpeakerOutputType(value)

  def set_speaker_output_type(self, output=None, new_type=None):
    assert new_type != None
    assert output != None
    # Here the index values are hardcoded and I don't use output 
    # because I noticed that they change which will be the left and right channel at the same time    
    self._set_value_on_device(self._OUTPUT_REQUESTS['SPEAKER_OUTPUT_TYPE'], 0, new_type.value)
    self._set_value_on_device(self._OUTPUT_REQUESTS['SPEAKER_OUTPUT_TYPE'], 1, new_type.value)

  #
  # Inputs
  #

  def _level_request(self, input_):
    request = self._INPUT_REQUESTS['LEVEL'].get(input_.type_)
    if request is None:
      raise ValueError('{} inputs have no gain register'.format(input_.type_))
    return request

  def get_input_level(self, input_=None):
    assert input_ != None
    value = self._get_value_from_device(self._level_request(input_), input_.index)
    return value
  
  def set_input_level(self, input_=None, level=None):
    assert level != None
    assert input_ != None
    self._set_value_on_device(self._level_request(input_), input_.index, level)
    
  def get_input_type(self, input_=None):
    assert input_ != None
    value = self._get_value_from_device(self._INPUT_REQUESTS['TYPE'], input_.index)
    return InputType(value)
  
  def set_input_type(self, input_=None, new_type=None):
    assert new_type != None
    assert input_ != None
    # The official app ungroups the inputs before changing the input type
    self.set_group_state(State.DISABLED)
    self._set_value_on_device(self._INPUT_REQUESTS['TYPE'], input_.index, new_type.value)
        
  def get_group_state(self, input_=None):
    assert input_ != None
    value = self._get_value_from_device(self._INPUT_REQUESTS['GROUP'], input_.index)
    return State(value)

  def set_group_state(self, state=None):
    # At least in the case of Apogee Duet, both input are grouped when clicking "group" in any of the two inputs
    for i in self.inputs:
      self._set_value_on_device(self._INPUT_REQUESTS['GROUP'], i.index, state.value)
      i.group_state = state

  def get_softlimit_state(self, input_=None):
    assert input_ != None
    value = self._get_value_from_device(self._INPUT_REQUESTS['SOFTLIMIT'], input_.index)
    return State(value)
    
  def set_softlimit_state(self, input_=None, state=None):
    self._set_value_on_device(self._INPUT_REQUESTS['SOFTLIMIT'], input_.index, state.value)
    
  def get_phase_state(self, input_=None):
    assert input_ != None
    value = self._get_value_from_device(self._INPUT_REQUESTS['PHASE'], input_.index)
    return State(value)
    
  def set_phase_state(self, input_=None, state=None):
    self._set_value_on_device(self._INPUT_REQUESTS['PHASE'], input_.index, state.value)
    
  def get_phantom_power_state(self, input_=None):
    assert input_ != None
    value = self._get_value_from_device(self._INPUT_REQUESTS['PHANTOM_POWER'], input_.index)
    return State(value)
  
  def set_phantom_power_state(self, input_=None, state=None):
    self._set_value_on_device(self._INPUT_REQUESTS['PHANTOM_POWER'], input_.index, state.value)

if __name__ == '__main__':
  import apogee_ui

  apogee_ui.run()
