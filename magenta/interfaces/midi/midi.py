# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A MIDI interface to the sequence generators.

Captures monophonic input MIDI sequences and plays back responses from the
sequence generator.
"""

import ast
import functools
from sys import stdout
import threading
import time
import copy

# internal imports
import mido
import tensorflow as tf

from magenta.lib import sequence_generator_bundle
from magenta.models.attention_rnn import attention_rnn_generator
from magenta.models.basic_rnn import basic_rnn_generator
from magenta.models.lookback_rnn import lookback_rnn_generator
from magenta.protobuf import generator_pb2
from magenta.protobuf import music_pb2

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_bool(
    'list',
    False,
    'Only list available MIDI ports.')
tf.app.flags.DEFINE_string(
    'input_port',
    None,
    'The name of the input MIDI port.')
tf.app.flags.DEFINE_string(
    'output_port',
    None,
    'The name of the output MIDI port.')
tf.app.flags.DEFINE_integer(
    'start_capture_control_number',
    1,
    'The control change number to use as a signal to start '
    'capturing. Defaults to modulation wheel.')
tf.app.flags.DEFINE_integer(
    'start_capture_control_value',
    127,
    'The control change value to use as a signal to start '
    'capturing. If None, any control change with '
    'start_capture_control_number will start capture.')
tf.app.flags.DEFINE_integer(
    'stop_capture_control_number',
    1,
    'The control change number to use as a signal to stop '
    'capturing and generate. Defaults to the modulation '
    'wheel.')
tf.app.flags.DEFINE_integer(
    'stop_capture_control_value',
    0,
    'The control change value to use as a signal to stop '
    'capturing and generate. If None, any control change with'
    'stop_capture_control_number will stop capture.')
# TODO(adarob): Make the qpm adjustable by a control change signal.
tf.app.flags.DEFINE_integer(
    'qpm',
    90,
    'The quarters per minute to use for the metronome and generated sequence.')
# TODO(adarob): Make the number of bars to generate adjustable.
tf.app.flags.DEFINE_integer(
    'num_bars_to_generate',
    5,
    'The number of bars to generate each time.')
tf.app.flags.DEFINE_integer(
    'metronome_channel',
    0,
    'The MIDI channel on which to send the metronome click.')
tf.app.flags.DEFINE_integer(
    'metronome_playback_velocity',
    0,
    'The velocity of the generated playback metronome '
    'expressed as an integer between 0 and 127.')
tf.app.flags.DEFINE_string(
    'bundle_file',
    None,
    'The location of the bundle file to use. If specified, generator_name, '
    'checkpoint, and hparams cannot be specified.')
tf.app.flags.DEFINE_string(
    'generator_name',
    None,
    'The name of the SequenceGenerator being used.')
tf.app.flags.DEFINE_string(
    'checkpoint',
    None,
    'The training directory with checkpoint files or the path to a single '
    'checkpoint file for the model being used.')
tf.app.flags.DEFINE_string(
    'hparams',
    '{}',
    'String representation of a Python dictionary containing hyperparameter to '
    'value mappings. This mapping is merged with the default hyperparameters.')
tf.app.flags.DEFINE_bool(
    'custom_cc',
    False,
    'Opens interactive control change assignment tool.')

# A map from a string generator name to its factory class.
_GENERATOR_FACTORY_MAP = {
    'attention_rnn': attention_rnn_generator,
    'basic_rnn': basic_rnn_generator,
    'lookback_rnn': lookback_rnn_generator,
}

_METRONOME_TICK_DURATION = 0.05
_METRONOME_PITCH = 95
_METRONOME_VELOCITY = 64
_METRONOME_DB_PITCH = 102
_METRONOME_DB_VELOCITY = 70
_QUARTER_PER_BAR = 4
_QPM = 90


_CUSTOM_CC_MAP = {
  'Start Capture': mido.Message('control_change', channel=0, control=1,
                                value=127),
  'Stop Capture': mido.Message('control_change', channel=0, control=1, value=0),
  'Metronome velocity': mido.Message('control_change', channel=0, control=60),
}

def serialized(func):
  """Decorator to provide mutual exclusion for method using _lock attribute."""
  @functools.wraps(func)
  def serialized_method(self, *args, **kwargs):
    lock = getattr(self, '_lock')
    with lock:
      return func(self, *args, **kwargs)
  return serialized_method


def stdout_write_and_flush(s):
  stdout.write(s)
  stdout.flush()


class GeneratorException(Exception):
  """An exception raised by the Generator class."""
  pass


class Generator(object):
  """A class wrapping a SequenceGenerator.

  Args:
    generator_name: The name of the generator to wrap. Must be present in
        _GENERATOR_FACTORY_MAP.
    num_bars_to_generate: The number of bars to generate on each call.
        Assumes 4/4 time.
    hparams: A Python dictionary containing hyperparameter to value mappings to
        be merged with the default hyperparameters.
    checkpoint: The training directory with checkpoint files or the path to a
        single checkpoint file for the model being used.
  Raises:
    GeneratorException: If an invalid generator name is given or no training
        directory is given.
  """

  def __init__(
      self,
      generator_name,
      num_bars_to_generate,
      hparams,
      checkpoint=None,
      bundle_file=None):
    self._num_bars_to_generate = num_bars_to_generate

    if not checkpoint and not bundle_file:
      raise GeneratorException(
          'No generator checkpoint or bundle location supplied.')
    if (checkpoint or generator_name or hparams) and bundle_file:
      raise GeneratorException(
          'Cannot specify both bundle file and checkpoint, generator_name, '
          'or hparams.')

    bundle = None
    if bundle_file:
      bundle = sequence_generator_bundle.read_bundle_file(bundle_file)
      generator_name = bundle.generator_details.id

    if generator_name not in _GENERATOR_FACTORY_MAP:
      raise GeneratorException('Invalid generator name given: %s',
                               generator_name)

    generator = _GENERATOR_FACTORY_MAP[generator_name].create_generator(
        checkpoint=checkpoint, bundle=bundle, hparams=hparams)
    generator.initialize()

    self._generator = generator

  def generate_melody(self, input_sequence):
    """Calls the SequenceGenerator and returns the generated NoteSequence."""
    # TODO(fjord): Align generation time on a measure boundary.
    notes_by_end_time = sorted(input_sequence.notes, key=lambda n: n.end_time)
    last_end_time = notes_by_end_time[-1].end_time if notes_by_end_time else 0

    # Assume 4/4 time signature and a single tempo.
    qpm = input_sequence.tempos[0].qpm
    seconds_to_generate = (60.0 / qpm) * 4 * self._num_bars_to_generate

    request = generator_pb2.GenerateSequenceRequest()
    request.input_sequence.CopyFrom(input_sequence)
    section = request.generator_options.generate_sections.add()
    # Start generating 1 quarter note after the sequence ends.
    section.start_time_seconds = last_end_time + (60.0 / qpm)
    section.end_time_seconds = section.start_time_seconds + seconds_to_generate

    response = self._generator.generate(request)
    return response.generated_sequence


class Metronome(threading.Thread):
  """A thread implementing a MIDI metronome.

  Attributes:
    _outport: The Mido port for sending messages.
    _qpm: The integer quarters per minute to signal on.
    _stop_metronome: A boolean specifying whether the metronome should stop.
  Args:
    outport: The Mido port for sending messages.
    bpm: The integer beats per minute to signal on.
    qpm: The integer quarters per minute to signal on.
  """
  daemon = True

  def __init__(self, outport, qpm, clock_start_time):
    self._outport = outport
    self._qpm = qpm
    self._stop_metronome = False
    self._clock_start_time = clock_start_time
    super(Metronome, self).__init__()

  def run(self):
    """Outputs metronome tone on the qpm interval until stop signal received."""
    global _TICK_COUNTER
    _TICK_COUNTER = 1.
    sleep_offset = 0
    sub_clock = 16
    while not self._stop_metronome:
      period = 60. / (self._qpm * sub_clock)
      now = time.time()
      next_tick_time = (now + period - ((now - self._clock_start_time) % period))
      delta = next_tick_time - time.time()
      if delta > 0:
        time.sleep(delta + sleep_offset)

      # The sleep function tends to return a little early or a little late.
      # Gradually modify an offset based on whether it returned early or late,
      # but prefer returning a little bit early.
      # If it returned early, spin until the correct time occurs.
      tick_late = time.time() - next_tick_time
      if tick_late > 0:
        sleep_offset -= .0005
      elif tick_late < -.001:
        sleep_offset += .0005

      if tick_late < 0:
        while time.time() < next_tick_time:
          pass

      if _TICK_COUNTER % _QUARTER_PER_BAR == 1:
        self._outport.send(mido.Message(type='note_on', note=_METRONOME_DB_PITCH,
                                        channel=FLAGS.metronome_channel,
                                        velocity=_METRONOME_DB_VELOCITY))
        time.sleep(_METRONOME_TICK_DURATION)
        self._outport.send(mido.Message(type='note_off', note=_METRONOME_DB_PITCH,
                                        channel=FLAGS.metronome_channel))
        _TICK_COUNTER += (1. / sub_clock)

      elif _TICK_COUNTER % 1 == 0:
        self._outport.send(mido.Message(type='note_on', note=_METRONOME_PITCH,
                                        channel=FLAGS.metronome_channel,
                                        velocity=_METRONOME_VELOCITY))
        time.sleep(_METRONOME_TICK_DURATION)
        self._outport.send(mido.Message(type='note_off', note=_METRONOME_PITCH,
                                        channel=FLAGS.metronome_channel))
        _TICK_COUNTER += (1. / sub_clock)
      else:
        _TICK_COUNTER += (1. / sub_clock)

  def stop(self):
    """Signals for the metronome to stop and joins thread."""
    self._stop_metronome = True
    self.join()


class MonoMidiPlayer(threading.Thread):
  """A thread for playing back a monophonic, sorted NoteSequence via MIDI.

  Attributes:
    _outport: The Mido port for sending messages.
    _sequence: The monohponic, chronologically sorted NoteSequence to play.
    _stop_playback: A boolean specifying whether the playback should stop.
  Args:
    outport: The Mido port for sending messages.
    sequence: The monohponic, chronologically sorted NoteSequence to play.
  Raises:
    ValueError: The NoteSequence contains multiple tempos.
  """
  daemon = True

  def __init__(self, outport, sequence, start_time):
    self._outport = outport
    self._sequence = sequence
    self._start_time = start_time
    self._stop_playback = False
    # if len(sequence.tempos) != 1:
    #   raise ValueError('The NoteSequence contains multiple tempos.')
    self._metronome = Metronome(self._outport, _QPM, time.time())
    super(MonoMidiPlayer, self).__init__()

  def run(self):
    """Plays back the NoteSequenceMIDI until it ends or stop signal is received.

    Raises:
      ValueError: The NoteSequence is not monophonic and chronologically sorted.
    """
    # Wall start time.
    play_start = time.time()
    # Time relative to start of NoteSequence.
    playhead = self._start_time


    for note in self._sequence:
      if self._stop_playback:
        self._outport.panic()
        return

      if note.time < playhead:
        pass
      if note.time >= playhead:
        playhead = note.time
        delta = (playhead - self._start_time) - (time.time() - play_start)
        if delta > 0:
          time.sleep(delta)
        self._outport.send(note)

    stdout_write_and_flush('Done\n')

  def stop(self):
    """Signals for the playback and metronome to stop and joins thread."""
    self._stop_playback = True
    # self._metronome.stop()
    self.join()


class MonoMidiHub(object):
  """A MIDI interface for capturing and playing monophonic NoteSequences.

  Attributes:
    _inport: The Mido port for receiving messages.
    _outport: The Mido port for sending messages.
    _lock: An RLock used for thread-safety.
    _capture_sequence: The NoteSequence being built from MIDI messages currently
        being captured or having been captured in the previous session.
    _control_cvs: A dictionary mapping (<control change number>,) and
        (<control change number>, <control change value>) to a condition
        variable that will be notified when a matching control change messsage
        is received.
    _player: A thread for playing back NoteSequences via the MIDI output port.
  Args:
    input_midi_port: The string MIDI port name to use for input.
    output_midi_port: The string MIDI port name to use for output.
  """

  def __init__(self, input_midi_port, output_midi_port):
    self._inport = mido.open_input(input_midi_port)
    self._outport = mido.open_output(output_midi_port)
    # This lock is used by the serialized decorator.
    self._lock = threading.RLock()
    self._control_cvs = dict()
    self._player = None
    self._capture_start_time = None
    self._sequence_start_time = None

  def _timestamp_and_capture_message(self, msg):
    """Stamps message with current time and passes it to the capture handler."""
    msg.time = time.time()
    self._capture_message(msg)

  @serialized
  def _capture_message(self, msg):
    """Handles a single incoming MIDI message during capture. Used as callback.

    If the message is a control change, notifies threads waiting on the
    appropriate condition variable.

    If the message is a note_on event, ends the previous note (if applicable)
    and opens a new note in the capture sequence. Also forwards the message to
    the output MIDI port. Ignores repeated note_on events.

    If the message is a note_off event matching the current open note in the
    capture sequence, ends that note and forwards the message to the output MIDI
    port.

    Args:
      msg: The mido.Message MIDI message to handle.
    """


    if msg == _CUSTOM_CC_MAP['Start Capture'] or msg == _CUSTOM_CC_MAP[
      'Stop Capture']:
      if msg.hex() in self._control_cvs:
        self._control_cvs[msg.hex()].notify_all()
      return

    for value in _CUSTOM_CC_MAP.values():
      if value is None:
        pass
      elif msg.bytes()[:2] == value.bytes()[:2]:
        self.execute_cc_message(msg)
        return

    # if (self._player is None) or (self._player.is_alive() is False):

    if msg.type == 'note_on' or msg.type == 'note_off':
      if msg.type == 'note_on' and msg.velocity > 0:
        if self._sequence_start_time is None:
          # This is the first note.
          # Find the sequence start time based on the start of the most recent
          # quarter note. This ensures that the sequence start time lines up
          # with a metronome tick.
          period = 60. / self.captured_sequence.tempos[0].qpm
          self._sequence_start_time = msg.time - (
              (msg.time - self._capture_start_time) % period)

        self._outport.send(msg)
        new_note = self.captured_sequence.notes.add()
        new_note.start_time = msg.time - self._sequence_start_time
        new_note.pitch = msg.note
        new_note.velocity = msg.velocity
        self.unclosed_notes[new_note.pitch] = self.note_index
        self.note_index += 1
        stdout_write_and_flush('.')

      elif msg.type == 'note_off' or (msg.type == 'note_on'
                                      and msg.velocity == 0):
        self._outport.send(msg)
        if msg.note in self.unclosed_notes:
          self.captured_sequence.notes[self.unclosed_notes
              [msg.note]].end_time = msg.time - self._sequence_start_time
          self.unclosed_notes.pop(msg.note)

  @serialized
  def start_capture(self, qpm):
    """Starts a capture session.

    Initializes a new capture sequence, sets the capture callback on the input
    port, and starts the metronome.

    Args:
      qpm: The integer quarters per minute to use for the metronome and captured
          sequence.
    Raises:
      RuntimeError: Already in a capture session.
    """

    self.captured_sequence = music_pb2.NoteSequence()
    self.captured_sequence.tempos.add().qpm = qpm
    self.unclosed_notes = {}
    self.note_index = 0
    self._sequence_start_time = None
    self._capture_start_time = time.time()
    self._inport.callback = self._timestamp_and_capture_message
    self._metronome = Metronome(self._outport, qpm, self._capture_start_time)
    self._metronome.start()

  def sequence2midi(self, sequence):
    """Convert sequence to linear midi format
    Args:
      sequence: the sequence to convert to midi messages
    Returns:
      A list of temporaly sorted midi messages
    """

    midictionary = {}
    for note in sequence.notes:
      msg_on = mido.Message('note_on', note=note.pitch, velocity=note.velocity, time=note.start_time)
      msg_off = mido.Message('note_off', note=note.pitch, velocity=note.velocity, time=note.end_time)
      midictionary[msg_on] = note.start_time
      midictionary[msg_off] = note.end_time

    sorted_msg = sorted(midictionary, key=midictionary.__getitem__)

    return sorted_msg, sequence.tempos[0]

  @serialized
  def flash_capture(self):
    """Stops the capture session and returns the captured sequence.

    Resets the capture callback on the input port, closes the final open note
    (if applicable), stops the metronome, and returns the captured sequence.

    Returns:
        The captured NoteSequence.
    Raises:
      RuntimeError: Not in a capture session.
    """

    captured_sequence_segment = self.captured_sequence

    for i in self.unclosed_notes.values():
      captured_sequence_segment.notes[i].end_time = time.time() - self._sequence_start_time

    return captured_sequence_segment

  @serialized
  def wait_for_control_signal(self, control_message):
    """Blocks until a specific control signal arrives.
    Args:
      control_message: The control change message.
    """
    if self._inport.callback is None:
      # No callback set for inport
      for msg in self._inport:
        if msg.hex() == control_message.hex():
          return
    else:
      if control_message.hex() not in self._control_cvs:
        self._control_cvs[control_message.hex()] = threading.Condition(
          self._lock)
      self._control_cvs[control_message.hex()].wait()

  def start_playback(self, sequence, start_time):
    """Plays the monophonic, sorted NoteSequence through the MIDI output port.

    Stops any previously playing sequences.

    Args:
      sequence: The monohponic, chronologically sorted NoteSequence to play.
      metronome_velocity: The velocity of the metronome's MIDI note_on message.
    """
    self.stop_playback()
    self._player = MonoMidiPlayer(self._outport, sequence, start_time)
    self._player.start()

  def stop_playback(self):
    """Stops any active sequence playback."""
    if self._player is not None and self._player.is_alive():
      self._player.stop()

  def execute_cc_message(self, message):
    """Defines how to treat non Start/Stop user defined CC messages."""

    # Metronome Velocity
    if message.bytes()[:2] == _CUSTOM_CC_MAP['Metronome velocity'].bytes()[:2]:
      global _METRONOME_VELOCITY
      _METRONOME_VELOCITY = message.bytes()[2]

class CCRemapper(object):
  """CC Message Remapping interface.

  Attributes:
    _inport: The Mido port for receiving messages.

  Args:
    input_midi_port: The string MIDI port name to use for input.
  """

  def __init__(self, input_midi_port):
    self._inport = mido.open_input(input_midi_port)

  def remapper_interface(self):
    """Asks for user input of which parameters to remap"""

    while True:
      print '0. None (Exit)'
      for i, CCparam in enumerate(_CUSTOM_CC_MAP.keys()):
        print '{}. {}'.format(i + 1, CCparam)
      try:
        cc_choice_index = int(raw_input("Which CC Parameters would "
                                        "you like to map?\n>>>"))
      except ValueError:
        print 'Please enter a number...'
        time.sleep(1)
        continue

      else:
        if cc_choice_index == 0:
          self._inport.close()
          break
        elif cc_choice_index < 0 or cc_choice_index > len(_CUSTOM_CC_MAP):
          print("There is no CC Parameter assigned to that "
                "number, please select from the list.")
          time.sleep(1)
          continue
        else:
          cc_choice = _CUSTOM_CC_MAP.keys()[cc_choice_index - 1]

      if cc_choice == 'Start Capture' or cc_choice == 'Stop Capture':
        self.remap_capture_message(self._inport, cc_choice,
                                   _CUSTOM_CC_MAP['Start Capture'],
                                   _CUSTOM_CC_MAP['Stop Capture'])
      else:
        self.remap_cc_message(self._inport, cc_choice)

  def remap_cc_message(self, input_port, cc_choice):
    """Defines how to remap control change messages for defined parameters.

    Args:
      input_port: The input port to receive the control change message.
      cc_choice: The _CUSTOM_CC_MAP dictionary key to assign a message to.
    """

    while True:
      while input_port.receive(block=False) is not None:
        pass
      print "What control or key would you like to assign to {}? " \
            "Please press one now...".format(cc_choice)
      msg = input_port.receive()
      if msg.hex().startswith('B'):
        _CUSTOM_CC_MAP[cc_choice] = msg
        time.sleep(1)
        return
      else:
        print('Sorry, I only accept MIDI CC messages for this parameter..')
        time.sleep(.5)
        continue

  def remap_capture_message(self, input_port, cc_choice, msg_start_capture,
                            msg_stop_capture):
    """Remap incoming messages for start and stop capture.

    Args:
      input_port: The input port to receive the control change message.
      cc_choice: The _CUSTOM_CC_MAP dictionary key to assign a message to.
      msg_start_capture: The currently assigned start capture message.
      msg_stop_capture: The currently assigned stop capture message.
    """

    while True:
      while input_port.receive(block=False) is not None:
        pass
      print "What control or key would you like to assign to {}? " \
            "Please press one now...".format(cc_choice)
      msg = input_port.receive()
      if msg.type == 'note_on':
        print('You assigned this parameter to a musical note so '
              'it will not be available for musical content')
        time.sleep(1)
        break
      elif msg.type == 'control_change':
        time.sleep(1)
        break
      else:
        print('Sorry, I only accept buttons outputting MIDI CC '
              'and note messages...try again')
        continue

    if cc_choice == 'Start Capture':
      if msg_stop_capture is None:
        msg_start_capture = msg
      elif msg == msg_stop_capture:
        if msg.type == 'note_on':
          msg_start_capture = msg
        elif msg.type == 'control_change':
          print ('You sent an identical CC message for Stop Capture, '
                 'this will act as a toggle between Start and Stop.')
          msg_start_capture = msg
      elif msg.hex()[:5] == msg_stop_capture.hex()[:5]:
        if msg.type == 'note_on':
          msg_start_capture = msg
        elif msg.type == 'control_change':
          print ('You sent a CC message with the same '
                 'controller but a different value...')
          print ('A message with max value (127) will start capture.')
          print ('A message with min value (0) will stop capture.')
          msg_start_capture = mido.parse(msg.bytes()[:2] + [127])
          msg_stop_capture = mido.parse(msg.bytes()[:2] + [0])
      else:
        msg_start_capture = msg

    elif cc_choice == 'Stop Capture':
      if msg_start_capture is None:
        msg_stop_capture = msg
      elif msg == msg_start_capture:
        if msg.type == 'note_on':
          msg_stop_capture = msg
        elif msg.type == 'control_change':
          print ('You sent an identical CC message for Start Capture, '
                 'this will act as a toggle between Start and Stop.')
          msg_stop_capture = msg
      elif msg.hex()[:5] == msg_start_capture.hex()[:5]:
        if msg.type == 'note_on':
          msg_stop_capture = msg
        elif msg.type == 'control_change':
          print ('You sent a CC message with the same '
                 'controller but a different value...')
          print ('A message with max value (127) will start capture.')
          print ('A message with min value (0) will stop capture.')
          msg_start_capture = mido.parse(msg.bytes()[:2] + [127])
          msg_stop_capture = mido.parse(msg.bytes()[:2] + [0])
      else:
        msg_stop_capture = msg

    _CUSTOM_CC_MAP['Start Capture'] = msg_start_capture
    _CUSTOM_CC_MAP['Stop Capture'] = msg_stop_capture


def main(unused_argv):
  if FLAGS.list:
    print "Input ports: '" + "', '".join(mido.get_input_names()) + "'"
    print "Output ports: '" + "', '".join(mido.get_output_names()) + "'"
    return

  if FLAGS.input_port is None or FLAGS.output_port is None:
    print '--inport_port and --output_port must be specified.'
    return

  _QPM = FLAGS.qpm

  if FLAGS.custom_cc:
    cc_remapper = CCRemapper(FLAGS.input_port)
    cc_remapper.remapper_interface()

  # TODO(hanzorama): Old CC capture system can be removed
  if (FLAGS.start_capture_control_number == FLAGS.stop_capture_control_number
      and
      (FLAGS.start_capture_control_value == FLAGS.stop_capture_control_value or
       FLAGS.start_capture_control_value is None or
       FLAGS.stop_capture_control_value is None)):
    print('If using the same number for --start_capture_control_number and '
          '--stop_capture_control_number, --start_capture_control_value and '
          '--stop_capture_control_value must both be defined and unique.')
    return

  if not 0 <= FLAGS.metronome_playback_velocity <= 127:
    print 'The metronome_playback_velocity must be an integer between 0 and 127'
    return

  generator = Generator(
      FLAGS.generator_name,
      FLAGS.num_bars_to_generate,
      ast.literal_eval(FLAGS.hparams if FLAGS.hparams else '{}'),
      FLAGS.checkpoint,
      FLAGS.bundle_file)
  hub = MonoMidiHub(FLAGS.input_port, FLAGS.output_port)

  #TODO(hanzorama) don't use globals
  global _TICK_COUNTER

  stdout_write_and_flush('Waiting for start control signal...\n')

  hub.wait_for_control_signal(_CUSTOM_CC_MAP['Start Capture'])
  hub.stop_playback()
  hub.start_capture(_QPM)
  stdout_write_and_flush('Capturing notes until stop control signal...')
  prev_sequence_length = 0
  first_iteration = True
  # Determines where in the bar the updated sequence is played from
  refresh_point = 1
  captured_sequence = hub.flash_capture()
  loop_start_time = time.time()

  while time.time() - loop_start_time < 60:
    while len(captured_sequence.notes) == prev_sequence_length \
            or (3.75 > _TICK_COUNTER % 4 > 1):
      captured_sequence = hub.flash_capture()
      continue

    prev_sequence_length = len(captured_sequence.notes)

    gen_start = time.time()
    generated_sequence = generator.generate_melody(captured_sequence)
    midi_msg, tempo = hub.sequence2midi(generated_sequence)
    stdout_write_and_flush('Response generation took '
                           '{} seconds'.format(time.time() - gen_start))

    if _TICK_COUNTER % 4 == refresh_point:
      while _TICK_COUNTER % 4 == refresh_point:
        continue
    while _TICK_COUNTER % 4 != refresh_point:
      continue

    if first_iteration is True:
      start_tick = _TICK_COUNTER
      first_iteration = False

    playback_start_time = (_TICK_COUNTER - start_tick) * 60. / _QPM


    hub.start_playback(midi_msg, playback_start_time)

    captured_sequence = hub.flash_capture()


if __name__ == '__main__':
  tf.app.run()
