# Magenta MIDI Interface

This interface allows you to connect to a model
[generator](/magenta/models/README.md#generators) via a MIDI controller
and synthesizer. These can be either "hard" or "soft" components.

Note that you can only interface with a trained models that have a
[SequenceGenerator](/magenta/music/sequence_generator.py)
 defined for them.

<p align="center">
  <img src="midi.png" alt="Sequence Diagram for the MIDI interface"/>
</p>

## Installing Dependencies

Before using the interface, you will need to install some
dependencies. We have provided instructions for both Macintosh OS X
and Ubuntu Linux.

For users of Macintosh OS X, the instructions below assume that you
have installed [Homebrew](http://brew.sh).

### Install RtMidi

The interface uses a python library called [mido](http://mido.readthedocs.io) to
interface your computer's MIDI hub. For it to work, you need to separately
install a backend library it can use to connect to your system. Below are
instructions for installing RtMidi.
**Ubuntu:**

```bash
$ sudo apt-get install build-essential libasound2-dev libjack-dev
$ pip install --pre python-rtmidi
```

**Mac:** `pip install --pre python-rtmidi`

### Install QjackCtl (Ubuntu Only)

[QjackCtl](http://qjackctl.sourceforge.net/) is a tool that provides a graphical
interface for the JACK hub on Ubuntu to allow you to easily route signals
between MIDI components. You can install it using `sudo apt-get install
qjackctl`.

### Connect/Install MIDI Controller

If you are using a hardware controller, attach it to the machine. If you do not
have one, you can install a software controller such as
[VMPK](http://vmpk.sourceforge.net/) by doing the following.

**Ubuntu:** Use the command `sudo apt-get install vmpk`.<br />
**Mac:** Download and install from the
[VMPK website](http://vmpk.sourceforge.net/#Download).

### Connect/Install MIDI Synthesizer

If you are using a hardware synthesizer, attach it to the machine. If you do not
have one, you can install a software synthesizer such as [FluidSynth]
(http://www.fluidsynth.org) using the following commands:

**Ubuntu:** `sudo apt-get install fluidsynth`<br />
**Mac:** `brew install fluidsynth`

If using FluidSynth, you will also want to install a decent soundfont. You can
install one by doing the following:

**Ubuntu:** Use the command `sudo apt-get install fluid-soundfont-gm`.<br />
**Mac:** Download the soundfont from
http://www.musescore.org/download/fluid-soundfont.tar.gz and unpack the SF2
file.

## Set Up

### Ubuntu

Launch `qjackctl`. You'll probably want to do it in its own screen/tab
since it will print status messages to the terminal. Once the GUI
appears, click the "Start" button.

If using a software controller, you can launch it in the background or in its
own screen/tab. Use `vmpk` to launch VMPK.

If using a software synth, you can launch it in the background or in its own
screen/tab. Launch FluidSynth with the recommended soundfont installed above
using:

```bash
$ fluidsynth /usr/share/sounds/sf2/FluidR3_GM.sf2
```

In the QjackCtl GUI, click the "Connect" button. In the "Audio" tab, select your
synthesizer from the list on the left (e.g., "fluidsynth") and select "system"
from the list on the right. Then click the "Connect" button at the bottom.

### Mac

If using a software controller (e.g., VMPK), launch it.

If using a software synth, launch it. Launch FluidSynth with the
recommended soundfont downloaded above using:

```bash
$ fluidsynth /path/to/sf2
```

## Launching the Interface

After completing the installation and set up steps above, build the interface
with:

```bash
$ bazel build //magenta/interfaces/midi:magenta_midi
```

Once built, have it list the available MIDI ports:

```bash
$ bazel-bin/magenta/interfaces/midi/magenta_midi --list_ports
```

You should see a list of available input and output ports, including both the
controller (e.g., "VMPK Output") and synthesizer (e.g., "FluidSynth virtual
port").

To use the midi interface, you must supply a trained model bundle (.mag file).
You can either download one from the links on our model pages (e.g.,
[Basic RNN](/magenta/models/basic_rnn/README.md),
[Lookback RNN](/magenta/models/lookback_rnn/README.md),
[Attention RNN](/magenta/models/attention_rnn/README.md), etc.) or create a
bundle file from one of your training checkpoints using the instructions on
the model page.

You will now start the interface with this command, supplying the location of
the .mag bundle file and any additional flags required by the interaction (see
below):

```bash
$ bazel-bin/magenta/interfaces/midi/magenta_midi \
  --input_port=<controller port> \
  --output_port=<synthesizer port> \
  --bundle_file=<bundle_file> \
  <additional interaction-specific args>
```

## Using the "Call and Response" Interaction

"Call and response" is a type of interaction where one participant (you) produce
a "call" phrase and the other participant (Magenta) produces a "response" phrase
based upon that "call".

When you start the interface, "call" phrase capture will begin immediately. You
will hear a metronome ticking and the keys will now produce sounds through your
audio output.

A requirement of this interaction is that you supply either `--phrase_bars` or
`--end_call_control_number`.

If you used the `--phrase_bars` flag, after the specified number of bars, the
metronome will stop and a generated response will be played. After the same
number of bars, a call phrase capture will begin again, and the process repeats.

If you used the `--end_call_control_number` flag, you will signal with that
control number and a value of 0 to end the call phrase. At the end of the
current bar, the metronome will stop and a generated response will be played
that is the same length as your call phrase. After the response completes, call
phrase capture will begin again, and the process repeats.

Assuming you're using the
[Attention RNN](/magenta/models/attention_rnn/README.md) bundle file and are
using VPMK and FluidSynth, your command might look like this:

```bash
$ bazel-bin/magenta/interfaces/midi/magenta_midi \
  --input_port="VMPK Output" \
  --output_port="FluidSynth virtual port" \
  --bundle_file=/tmp/attention_rnn.mag \
  --phrase_bars=4
```
