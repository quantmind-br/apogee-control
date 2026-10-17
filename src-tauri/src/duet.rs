//! USB protocol and device state for the Apogee Duet (USB).
//!
//! Every setting is a one byte register behind a vendor control transfer on
//! endpoint 0: reads (`0xc0`) return the byte, writes (`0x40`) send it.
//! `wValue` is always zero and `wIndex` selects the input, output or mixer
//! channel the register belongs to.
//!
//! The device cannot report its layout, so the two inputs, the two outputs
//! and the four mixer channels are hard coded.

use std::time::Duration;

use rusb::{DeviceHandle, GlobalContext};
use serde::{Deserialize, Serialize, Serializer};

pub const VENDOR_ID: u16 = 0x0c60;
pub const PRODUCT_ID: u16 = 0x0016;

const READ: u8 = 0xc0;
const WRITE: u8 = 0x40;
const TIMEOUT: Duration = Duration::from_secs(1);

// The Duet drops roughly one control transfer in a thousand with an I/O
// error. Every register holds an absolute value rather than a delta, so
// re-sending a failed transfer cannot double apply it.
const TRANSFER_ATTEMPTS: usize = 2;

const INPUTS: usize = 2;
const OUTPUTS: usize = 2;
const CHANNELS: usize = 4;
const SPEAKERS: usize = 0;
const SOFTWARE_RETURN: usize = 2;
const MASTER: usize = 3;

pub const OUTPUT_LEVEL: (i16, i16) = (-64, 0);
pub const CHANNEL_LEVEL: (i16, i16) = (-48, 6);
pub const PAN: (i16, i16) = (-64, 64);

mod request {
  pub const INPUT_SOFT_LIMIT: u8 = 17;
  pub const INPUT_PHASE: u8 = 19;
  pub const INPUT_PHANTOM_POWER: u8 = 21;
  pub const INPUT_TYPE: u8 = 22;
  pub const MICROPHONE_GAIN: u8 = 52;
  pub const INSTRUMENT_GAIN: u8 = 62;
  pub const INPUT_GROUP: u8 = 68;

  pub const OUTPUT_LEVEL: u8 = 51;
  pub const OUTPUT_MUTE: u8 = 53;
  pub const OUTPUT_DIM: u8 = 64;
  pub const OUTPUT_MONO: u8 = 70;
  pub const OUTPUT_SOURCE: u8 = 83;
  pub const SPEAKER_OUTPUT_TYPE: u8 = 182;

  pub const SOFTWARE_RETURN_SOURCE: u8 = 54;
  pub const CHANNEL_LEVEL: u8 = 76;
  pub const CHANNEL_PAN: u8 = 77;
  pub const CHANNEL_SOLO: u8 = 78;
  pub const CHANNEL_MUTE: u8 = 79;
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
  #[error("Apogee Duet not found")]
  NotFound,
  #[error("No permission to open the Apogee Duet")]
  Access,
  #[error("The Apogee Duet was disconnected")]
  Disconnected,
  #[error("USB transfer failed: {0}")]
  Usb(rusb::Error),
  #[error("Register {request} returned {value}, which is not a known value")]
  UnknownValue { request: u8, value: u8 },
  #[error("{0}")]
  Invalid(&'static str),
}

impl Error {
  fn code(&self) -> &'static str {
    match self {
      Error::NotFound => "not_found",
      Error::Access => "access",
      Error::Disconnected => "disconnected",
      Error::Usb(_) | Error::UnknownValue { .. } => "usb",
      Error::Invalid(_) => "invalid",
    }
  }

  /// Whether the handle is unusable and the device has to be opened again.
  pub fn is_fatal(&self) -> bool {
    matches!(self, Error::Disconnected | Error::Access)
  }
}

impl From<rusb::Error> for Error {
  fn from(error: rusb::Error) -> Self {
    match error {
      rusb::Error::NoDevice => Error::Disconnected,
      rusb::Error::Access => Error::Access,
      error => Error::Usb(error),
    }
  }
}

// The frontend picks the offline page or a toast from `code`.
impl Serialize for Error {
  fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
    use serde::ser::SerializeStruct;
    let mut error = serializer.serialize_struct("Error", 2)?;
    error.serialize_field("code", self.code())?;
    error.serialize_field("message", &self.to_string())?;
    error.end()
  }
}

/// Register backed enumeration: the discriminant is the register value.
macro_rules! register_enum {
  ($(#[$meta:meta])* $name:ident { $($variant:ident = $value:literal => $wire:literal),+ $(,)? }) => {
    $(#[$meta])*
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
    pub enum $name {
      $(#[serde(rename = $wire)] $variant = $value),+
    }

    impl $name {
      fn from_register(request: u8, value: u8) -> Result<Self, Error> {
        match value {
          $($value => Ok(Self::$variant),)+
          _ => Err(Error::UnknownValue { request, value }),
        }
      }
    }
  };
}

register_enum! {
  InputType {
    Line4dBu = 0 => "line_4dbu",
    Line10dBV = 1 => "line_10dbv",
    Microphone = 2 => "microphone",
    Instrument = 3 => "instrument",
  }
}

register_enum! {
  OutputSource {
    Playback12 = 0 => "playback_1_2",
    Playback34 = 1 => "playback_3_4",
    Mixer = 2 => "mixer",
  }
}

register_enum! {
  SpeakerOutputType {
    Line4dBu = 0 => "line_4dbu",
    Line10dBV = 1 => "line_10dbv",
  }
}

register_enum! {
  SoftwareReturnSource {
    Playback12 = 0 => "playback_1_2",
    Playback34 = 1 => "playback_3_4",
  }
}

impl InputType {
  /// The line types bypass the preamp: the signal reaches the converters at a
  /// fixed reference level, so only the mic and instrument types have a gain
  /// register.
  fn gain(self) -> Option<(u8, u8)> {
    match self {
      InputType::Microphone => Some((request::MICROPHONE_GAIN, 75)),
      InputType::Instrument => Some((request::INSTRUMENT_GAIN, 65)),
      InputType::Line4dBu | InputType::Line10dBV => None,
    }
  }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InputState {
  pub input_type: InputType,
  /// Preamp gain in dB, absent for the line types.
  pub gain: Option<u8>,
  pub gain_max: Option<u8>,
  pub phantom_power: bool,
  pub phase: bool,
  pub soft_limit: bool,
  pub group: bool,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OutputState {
  /// Level in dB, from -64 to 0.
  pub level: i16,
  pub mute: bool,
  pub dim: bool,
  pub mono: bool,
  pub source: OutputSource,
  /// Only the speaker output has a selectable reference level.
  pub speaker_output_type: Option<SpeakerOutputType>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ChannelKind {
  Input,
  SoftwareReturn,
  Master,
}

impl ChannelKind {
  fn of(channel: usize) -> Self {
    match channel {
      SOFTWARE_RETURN => ChannelKind::SoftwareReturn,
      MASTER => ChannelKind::Master,
      _ => ChannelKind::Input,
    }
  }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ChannelState {
  pub kind: ChannelKind,
  /// Level in dB, from -48 to +6.
  pub level: i16,
  /// Pan from -64 (left) to 64 (right), input channels only.
  pub pan: Option<i16>,
  /// Mute and solo do not exist on the master bus.
  pub mute: Option<bool>,
  pub solo: Option<bool>,
  pub source: Option<SoftwareReturnSource>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Limits {
  pub output_level: (i16, i16),
  pub channel_level: (i16, i16),
  pub pan: (i16, i16),
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceState {
  pub product: Option<String>,
  pub serial: Option<String>,
  pub inputs: Vec<InputState>,
  pub outputs: Vec<OutputState>,
  pub mixer: Vec<ChannelState>,
  pub limits: Limits,
}

/// One setting to write. Toggles carry the target value instead of flipping
/// the current one, so a repeated or coalesced change is harmless.
#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum Change {
  InputGain { input: usize, value: u8 },
  InputType { input: usize, value: InputType },
  PhantomPower { input: usize, value: bool },
  Phase { input: usize, value: bool },
  SoftLimit { input: usize, value: bool },
  Group { value: bool },
  OutputLevel { output: usize, value: i16 },
  OutputMute { output: usize, value: bool },
  OutputDim { output: usize, value: bool },
  OutputMono { output: usize, value: bool },
  OutputSource { output: usize, value: OutputSource },
  SpeakerOutputType { value: SpeakerOutputType },
  ChannelLevel { channel: usize, value: i16 },
  ChannelPan { channel: usize, value: i16 },
  ChannelMute { channel: usize, value: bool },
  ChannelSolo { channel: usize, value: bool },
  SoftwareReturnSource { value: SoftwareReturnSource },
}

// Device registers store the output level as an attenuation, the channel
// level offset from its minimum and the pan offset from hard left.

fn output_level_from_register(value: u8) -> i16 {
  -i16::from(value)
}

fn output_level_to_register(level: i16) -> u8 {
  (-level) as u8
}

fn channel_level_from_register(value: u8) -> i16 {
  i16::from(value) + CHANNEL_LEVEL.0
}

fn channel_level_to_register(level: i16) -> u8 {
  (level - CHANNEL_LEVEL.0) as u8
}

fn pan_from_register(value: u8) -> i16 {
  i16::from(value) - PAN.1
}

fn pan_to_register(pan: i16) -> u8 {
  (pan + PAN.1) as u8
}

fn check_range(value: i16, (low, high): (i16, i16), what: &'static str) -> Result<i16, Error> {
  if (low..=high).contains(&value) { Ok(value) } else { Err(Error::Invalid(what)) }
}

fn check_index(index: usize, count: usize, what: &'static str) -> Result<usize, Error> {
  if index < count { Ok(index) } else { Err(Error::Invalid(what)) }
}

pub struct Duet {
  handle: DeviceHandle<GlobalContext>,
  state: DeviceState,
}

impl Duet {
  /// Opens the first attached Duet and reads its full state.
  pub fn open() -> Result<Self, Error> {
    let device = rusb::devices()?
      .iter()
      .find(|device| {
        device
          .device_descriptor()
          .is_ok_and(|descriptor| descriptor.vendor_id() == VENDOR_ID && descriptor.product_id() == PRODUCT_ID)
      })
      .ok_or(Error::NotFound)?;
    let descriptor = device.device_descriptor()?;
    let handle = device.open()?;
    // The descriptive strings live in USB string descriptors, which some
    // devices refuse to hand out; the panel can live without them.
    let product = handle.read_product_string_ascii(&descriptor).ok();
    let serial = handle.read_serial_number_string_ascii(&descriptor).ok();

    let mut duet = Duet {
      handle,
      state: DeviceState {
        product,
        serial,
        inputs: Vec::new(),
        outputs: Vec::new(),
        mixer: Vec::new(),
        limits: Limits { output_level: OUTPUT_LEVEL, channel_level: CHANNEL_LEVEL, pan: PAN },
      },
    };
    duet.refresh()?;
    Ok(duet)
  }

  pub fn state(&self) -> &DeviceState {
    &self.state
  }

  /// Reads every register again, picking up changes made on the device.
  pub fn refresh(&mut self) -> Result<&DeviceState, Error> {
    let inputs = (0..INPUTS).map(|input| self.read_input(input)).collect::<Result<_, _>>()?;
    let outputs = (0..OUTPUTS).map(|output| self.read_output(output)).collect::<Result<_, _>>()?;
    let mixer = (0..CHANNELS).map(|channel| self.read_channel(channel)).collect::<Result<_, _>>()?;
    self.state.inputs = inputs;
    self.state.outputs = outputs;
    self.state.mixer = mixer;
    Ok(&self.state)
  }

  /// Re-reads the registers that change outside the panel: the front knob
  /// moves the output levels and the input gain, and the speaker level and
  /// mute double as the USB Audio Class volume the system mixer drives.
  pub fn poll(&mut self) -> Result<&DeviceState, Error> {
    for output in 0..OUTPUTS {
      let level = output_level_from_register(self.read(request::OUTPUT_LEVEL, output)?);
      let mute = self.read_bool(request::OUTPUT_MUTE, output)?;
      let state = &mut self.state.outputs[output];
      state.level = level;
      state.mute = mute;
    }
    for input in 0..INPUTS {
      if let Some((request, _)) = self.state.inputs[input].input_type.gain() {
        self.state.inputs[input].gain = Some(self.read(request, input)?);
      }
    }
    Ok(&self.state)
  }

  /// Writes one setting and returns the updated state.
  pub fn apply(&mut self, change: Change) -> Result<&DeviceState, Error> {
    match change {
      Change::InputGain { input, value } => {
        let input = check_index(input, INPUTS, "No such input")?;
        let (request, max) = self.state.inputs[input]
          .input_type
          .gain()
          .ok_or(Error::Invalid("Line inputs have no gain control"))?;
        if value > max {
          return Err(Error::Invalid("Gain out of range"));
        }
        self.write(request, input, value)?;
        self.state.inputs[input].gain = Some(value);
      }
      Change::InputType { input, value } => {
        let input = check_index(input, INPUTS, "No such input")?;
        // The official app ungroups the inputs before changing the type.
        self.write_group(false)?;
        self.write(request::INPUT_TYPE, input, value as u8)?;
        // The type selects another gain register, and leaving microphone mode
        // drops the input out of phantom power.
        let gain = value.gain().map(|(request, _)| self.read(request, input)).transpose()?;
        let phantom_power = value == InputType::Microphone && self.read_bool(request::INPUT_PHANTOM_POWER, input)?;
        let state = &mut self.state.inputs[input];
        state.input_type = value;
        state.gain = gain;
        state.gain_max = value.gain().map(|(_, max)| max);
        state.phantom_power = phantom_power;
      }
      Change::PhantomPower { input, value } => {
        let input = check_index(input, INPUTS, "No such input")?;
        if self.state.inputs[input].input_type != InputType::Microphone {
          return Err(Error::Invalid("Phantom power needs the microphone input type"));
        }
        self.write(request::INPUT_PHANTOM_POWER, input, value.into())?;
        self.state.inputs[input].phantom_power = value;
      }
      Change::Phase { input, value } => {
        let input = check_index(input, INPUTS, "No such input")?;
        self.write(request::INPUT_PHASE, input, value.into())?;
        self.state.inputs[input].phase = value;
      }
      Change::SoftLimit { input, value } => {
        let input = check_index(input, INPUTS, "No such input")?;
        self.write(request::INPUT_SOFT_LIMIT, input, value.into())?;
        self.state.inputs[input].soft_limit = value;
      }
      Change::Group { value } => self.write_group(value)?,
      Change::OutputLevel { output, value } => {
        let output = check_index(output, OUTPUTS, "No such output")?;
        let level = check_range(value, OUTPUT_LEVEL, "Output level out of range")?;
        self.write(request::OUTPUT_LEVEL, output, output_level_to_register(level))?;
        self.state.outputs[output].level = level;
      }
      Change::OutputMute { output, value } => {
        let output = check_index(output, OUTPUTS, "No such output")?;
        self.write(request::OUTPUT_MUTE, output, value.into())?;
        self.state.outputs[output].mute = value;
      }
      Change::OutputDim { output, value } => {
        let output = check_index(output, OUTPUTS, "No such output")?;
        self.write(request::OUTPUT_DIM, output, value.into())?;
        self.state.outputs[output].dim = value;
      }
      Change::OutputMono { output, value } => {
        let output = check_index(output, OUTPUTS, "No such output")?;
        self.write(request::OUTPUT_MONO, output, value.into())?;
        self.state.outputs[output].mono = value;
      }
      Change::OutputSource { output, value } => {
        let output = check_index(output, OUTPUTS, "No such output")?;
        self.write(request::OUTPUT_SOURCE, output, value as u8)?;
        self.state.outputs[output].source = value;
      }
      Change::SpeakerOutputType { value } => {
        // Indexes 0 and 1 are the left and right speaker channels; the
        // official app always switches both together.
        self.write(request::SPEAKER_OUTPUT_TYPE, 0, value as u8)?;
        self.write(request::SPEAKER_OUTPUT_TYPE, 1, value as u8)?;
        self.state.outputs[SPEAKERS].speaker_output_type = Some(value);
      }
      Change::ChannelLevel { channel, value } => {
        let channel = check_index(channel, CHANNELS, "No such mixer channel")?;
        let level = check_range(value, CHANNEL_LEVEL, "Mixer level out of range")?;
        self.write(request::CHANNEL_LEVEL, channel, channel_level_to_register(level))?;
        self.state.mixer[channel].level = level;
      }
      Change::ChannelPan { channel, value } => {
        let channel = check_index(channel, CHANNELS, "No such mixer channel")?;
        if ChannelKind::of(channel) != ChannelKind::Input {
          return Err(Error::Invalid("Only input channels can be panned"));
        }
        let pan = check_range(value, PAN, "Pan out of range")?;
        self.write(request::CHANNEL_PAN, channel, pan_to_register(pan))?;
        self.state.mixer[channel].pan = Some(pan);
      }
      Change::ChannelMute { channel, value } => {
        let channel = self.check_soloable(channel)?;
        self.write(request::CHANNEL_MUTE, channel, value.into())?;
        self.state.mixer[channel].mute = Some(value);
      }
      Change::ChannelSolo { channel, value } => {
        let channel = self.check_soloable(channel)?;
        self.write(request::CHANNEL_SOLO, channel, value.into())?;
        self.state.mixer[channel].solo = Some(value);
      }
      Change::SoftwareReturnSource { value } => {
        self.write(request::SOFTWARE_RETURN_SOURCE, SOFTWARE_RETURN, value as u8)?;
        self.state.mixer[SOFTWARE_RETURN].source = Some(value);
      }
    }
    Ok(&self.state)
  }

  fn check_soloable(&self, channel: usize) -> Result<usize, Error> {
    let channel = check_index(channel, CHANNELS, "No such mixer channel")?;
    if channel == MASTER {
      return Err(Error::Invalid("The master bus has no mute or solo"));
    }
    Ok(channel)
  }

  /// Grouping is device wide: both inputs always share the state.
  fn write_group(&mut self, value: bool) -> Result<(), Error> {
    for input in 0..INPUTS {
      self.write(request::INPUT_GROUP, input, value.into())?;
      if let Some(state) = self.state.inputs.get_mut(input) {
        state.group = value;
      }
    }
    Ok(())
  }

  fn read_input(&self, input: usize) -> Result<InputState, Error> {
    let input_type = InputType::from_register(request::INPUT_TYPE, self.read(request::INPUT_TYPE, input)?)?;
    let gain = input_type.gain().map(|(request, _)| self.read(request, input)).transpose()?;
    Ok(InputState {
      input_type,
      gain,
      gain_max: input_type.gain().map(|(_, max)| max),
      phantom_power: input_type == InputType::Microphone && self.read_bool(request::INPUT_PHANTOM_POWER, input)?,
      phase: self.read_bool(request::INPUT_PHASE, input)?,
      soft_limit: self.read_bool(request::INPUT_SOFT_LIMIT, input)?,
      group: self.read_bool(request::INPUT_GROUP, input)?,
    })
  }

  fn read_output(&self, output: usize) -> Result<OutputState, Error> {
    let speaker_output_type = if output == SPEAKERS {
      // Both speaker channels are always switched together; read the left one.
      let value = self.read(request::SPEAKER_OUTPUT_TYPE, 0)?;
      Some(SpeakerOutputType::from_register(request::SPEAKER_OUTPUT_TYPE, value)?)
    } else {
      None
    };
    Ok(OutputState {
      level: output_level_from_register(self.read(request::OUTPUT_LEVEL, output)?),
      mute: self.read_bool(request::OUTPUT_MUTE, output)?,
      dim: self.read_bool(request::OUTPUT_DIM, output)?,
      mono: self.read_bool(request::OUTPUT_MONO, output)?,
      source: OutputSource::from_register(request::OUTPUT_SOURCE, self.read(request::OUTPUT_SOURCE, output)?)?,
      speaker_output_type,
    })
  }

  fn read_channel(&self, channel: usize) -> Result<ChannelState, Error> {
    let kind = ChannelKind::of(channel);
    let soloable = kind != ChannelKind::Master;
    Ok(ChannelState {
      kind,
      level: channel_level_from_register(self.read(request::CHANNEL_LEVEL, channel)?),
      pan: match kind {
        ChannelKind::Input => Some(pan_from_register(self.read(request::CHANNEL_PAN, channel)?)),
        _ => None,
      },
      mute: soloable.then(|| self.read_bool(request::CHANNEL_MUTE, channel)).transpose()?,
      solo: soloable.then(|| self.read_bool(request::CHANNEL_SOLO, channel)).transpose()?,
      source: match kind {
        ChannelKind::SoftwareReturn => {
          let value = self.read(request::SOFTWARE_RETURN_SOURCE, channel)?;
          Some(SoftwareReturnSource::from_register(request::SOFTWARE_RETURN_SOURCE, value)?)
        }
        _ => None,
      },
    })
  }

  fn read_bool(&self, request: u8, index: usize) -> Result<bool, Error> {
    Ok(self.read(request, index)? != 0)
  }

  fn read(&self, request: u8, index: usize) -> Result<u8, Error> {
    let mut buffer = [0u8; 1];
    let count = retry(|| self.handle.read_control(READ, request, 0, index as u16, &mut buffer, TIMEOUT))?;
    if count != buffer.len() {
      return Err(Error::Usb(rusb::Error::Io));
    }
    Ok(buffer[0])
  }

  fn write(&self, request: u8, index: usize, value: u8) -> Result<(), Error> {
    let count = retry(|| self.handle.write_control(WRITE, request, 0, index as u16, &[value], TIMEOUT))?;
    if count != 1 {
      return Err(Error::Usb(rusb::Error::Io));
    }
    Ok(())
  }
}

fn retry<T>(mut transfer: impl FnMut() -> rusb::Result<T>) -> Result<T, Error> {
  let mut result = transfer();
  for _ in 1..TRANSFER_ATTEMPTS {
    match result {
      Err(rusb::Error::NoDevice | rusb::Error::Access) | Ok(_) => break,
      Err(_) => result = transfer(),
    }
  }
  Ok(result?)
}

#[cfg(test)]
mod tests {
  use super::*;

  #[test]
  fn output_level_is_stored_as_attenuation() {
    assert_eq!(output_level_to_register(-64), 64);
    assert_eq!(output_level_to_register(0), 0);
    assert_eq!(output_level_from_register(20), -20);
  }

  #[test]
  fn channel_level_is_offset_from_minimum() {
    assert_eq!(channel_level_to_register(-48), 0);
    assert_eq!(channel_level_to_register(6), 54);
    assert_eq!(channel_level_from_register(48), 0);
  }

  #[test]
  fn pan_is_offset_from_hard_left() {
    assert_eq!(pan_to_register(-64), 0);
    assert_eq!(pan_to_register(0), 64);
    assert_eq!(pan_from_register(128), 64);
  }

  #[test]
  fn line_inputs_have_no_gain() {
    assert_eq!(InputType::Line4dBu.gain(), None);
    assert_eq!(InputType::Line10dBV.gain(), None);
    assert_eq!(InputType::Microphone.gain(), Some((request::MICROPHONE_GAIN, 75)));
    assert_eq!(InputType::Instrument.gain(), Some((request::INSTRUMENT_GAIN, 65)));
  }

  #[test]
  fn unknown_register_values_are_rejected() {
    assert!(matches!(
      InputType::from_register(request::INPUT_TYPE, 4),
      Err(Error::UnknownValue { request: request::INPUT_TYPE, value: 4 })
    ));
    assert_eq!(OutputSource::from_register(request::OUTPUT_SOURCE, 2).unwrap(), OutputSource::Mixer);
  }

  #[test]
  fn range_checks() {
    assert!(check_range(-65, OUTPUT_LEVEL, "").is_err());
    assert!(check_range(7, CHANNEL_LEVEL, "").is_err());
    assert_eq!(check_range(-64, PAN, "").unwrap(), -64);
  }

  #[test]
  fn retry_repeats_transient_errors_once() {
    let mut calls = 0;
    let result = retry(|| {
      calls += 1;
      if calls == 1 { Err(rusb::Error::Io) } else { Ok(1) }
    });
    assert_eq!(result.unwrap(), 1);
    assert_eq!(calls, 2);
  }

  #[test]
  fn retry_gives_up_on_disconnect() {
    let mut calls = 0;
    let result: Result<usize, _> = retry(|| {
      calls += 1;
      Err(rusb::Error::NoDevice)
    });
    assert!(matches!(result, Err(Error::Disconnected)));
    assert_eq!(calls, 1);
  }

  /// Needs an attached Duet: `cargo test -- --ignored`. Nudges the headphone
  /// level by 1 dB and puts it back.
  #[test]
  #[ignore]
  fn hardware_round_trip() {
    let mut duet = Duet::open().expect("open the Duet");
    let level = duet.state().outputs[1].level;
    let nudged = if level > OUTPUT_LEVEL.0 { level - 1 } else { level + 1 };
    duet.apply(Change::OutputLevel { output: 1, value: nudged }).unwrap();
    assert_eq!(duet.poll().unwrap().outputs[1].level, nudged);
    duet.apply(Change::OutputLevel { output: 1, value: level }).unwrap();
    assert_eq!(duet.refresh().unwrap().outputs[1].level, level);
  }

  #[test]
  fn changes_deserialize_from_tagged_json() {
    let change: Change = serde_json::from_str(r#"{"kind":"inputType","input":1,"value":"instrument"}"#).unwrap();
    assert!(matches!(change, Change::InputType { input: 1, value: InputType::Instrument }));
    let change: Change = serde_json::from_str(r#"{"kind":"channelPan","channel":0,"value":-12}"#).unwrap();
    assert!(matches!(change, Change::ChannelPan { channel: 0, value: -12 }));
  }
}
