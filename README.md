# reMarkable MPE MIDI Controller

Turn your reMarkable tablet (Paper Pro, Paper Pro Move, reMarkable 2, or reMarkable 1) into a high-precision, pressure-sensitive **MIDI Polyphonic Expression (MPE)** controller for Linux.

This script sniffs raw digitizer and multi-touch hardware events directly from the tablet over SSH and translates them into MIDI notes, pitch bend glides, and CC modulations, registering a virtual ALSA MIDI output port on your host Linux PC.

This started as a fun "Is it even possible?" experiment, and it turns out it absolutely is. Have fun turning your writing tablet into a musical instrument!

---

## Features

*   **Continuous Pitch Glide (ROLI Seaboard / Haken Continuum Style):**
    Horizontal movement (X-axis) triggers notes dynamically based on coordinate strikes. Sliding left and right continuously bends the pitch across multiple octaves without re-triggering the note.
*   **Vertical Timbre Slide:**
    Sliding vertically (Y-axis) maps to standard MPE Timbre (CC 74), giving you dynamic per-finger filter sweeps, wavefolding, or volume modulation.
*   **Per-Note Aftertouch / Pressure:**
    - **Stylus:** Maps physical pen tip pressure (`ABS_PRESSURE`) directly to Channel Pressure (aftertouch) or CC modulation.
    - **Fingers:** Maps capacitive contact surface size (`ABS_MT_TOUCH_MAJOR`) to aftertouch (very low resolution).
*   **3D Pen Expression:**
    Supports physical pen tilt (Tilt X and Y) mapped to independent CC parameters (CC 16 & 17 by default) for a third and fourth dimension of sound modulation.
*   **Dynamic MPE Channel Allocation:**
    Round-robin assigns touch fingers and the pen to dedicated MIDI channels (Channels 2–16) for polyphonic voice isolation. Automatically initializes the synthesizer with MPE Configuration RPN messages.
*   **Toggleable Palm Rejection:**
    - `--palm-rejection` (Default): stylus proximity cancels active touch contacts to avoid resting palm noise.
    - `--no-palm-rejection`: disables touch suppression, allowing you to hold chords on the touchscreen with one hand while playing solos/sweeps with the pen in the other hand (without this hand touching the screen).

---

## Installation

### 1. Install Dependencies

The driver runs on your Linux host PC. It requires `mido`, `python-rtmidi`, `paramiko`, and `rich`.

Since `python-rtmidi` contains a compiled C++ extension, installing it on newer Python versions (such as Python 3.13) may require a compiler and ALSA development headers if pre-compiled wheels are not found.

#### Inside a Conda/Mamba Environment (Recommended for OSTree-based immutable OS):
If you have a conda/mamba setup, you can install the compiler tools and ALSA headers directly inside your environment:

```bash
# 1. Install g++ compiler and alsa development headers
mamba install -y -c conda-forge -n mamba313 gxx_linux-64 alsa-lib

# 2. Install Python dependencies (this compiles python-rtmidi automatically)
mamba run -n mamba313 pip install -r requirements.txt
```

#### On Debian/Ubuntu (Standard System):
```bash
sudo apt install build-essential libasound2-dev
pip install -r requirements.txt
```

#### On Fedora/RHEL (Standard System):
```bash
sudo dnf install gcc-c++ alsa-lib-devel
pip install -r requirements.txt
```

---

## Usage

### 1. Setup Virtual Raw MIDI (Required for Sandboxed DAWs / Flatpak / Distrobox)
If your DAW does not see standard ALSA virtual sequencer ports, you can load the Linux kernel's virtual raw MIDI driver to create a hardware-level virtual device that containerized apps can see:
```bash
sudo modprobe snd-virmidi enable=1,0,0,0 midi_devs=1
```
To load this device automatically on every boot:
```bash
echo "options snd-virmidi enable=1,0,0,0 midi_devs=1" | sudo tee /etc/modprobe.d/snd-virmidi.conf
echo "snd-virmidi" | sudo tee /etc/modules-load.d/snd-virmidi.conf
```

### 2. Start the Controller

Run the driver using your configured environment. By default, it searches for a `Virtual Raw MIDI` port (falling back to `Midi Through` if not found) and connects to the tablet over SSH:

```bash
./mpe_driver.py --freeze
```

*   **The `--freeze` flag:** Temporarily stops the tablet's user-interface (`xochitl`), freezing the screen on whatever you had open (e.g. a PDF/notebook of a piano keys layout). This disables all physical touch/drag gestures (zooming, panning) and prevents accidental scribbles while playing. Pressing `Ctrl+C` to exit the driver automatically unfreezes the screen.

### 3. Connect to a Synthesizer / DAW

1.  Open an MPE-compatible software synthesizer (such as **Vital**, **Surge XT**, or **Polymer**) or your DAW.
2.  Select **`Virtual Raw MIDI 2-0`** (or the port auto-selected by the script) in your MIDI input preferences and enable MPE.
3.  **Align Pitch Bend Range (Crucial):** By default, the script automatically scales your Pitch Bend range to match the screen's octaves (`12 * octaves`). E.g., for the default 4 octaves, the Pitch Bend range is **48 semitones**. You **must** go into your synthesizer plugin's MPE settings and set its internal MPE Pitch Bend range to the exact same value. Otherwise, your physical slides will not track the notes accurately.
4.  Optionally, use ALSA tools to monitor or route messages manually:
    ```bash
    # View all midi ports
    aconnect -l
    
    # Dump midi messages from a specific port
    aseqdump -p "Virtual Raw MIDI 2-0"
    ```

---

## Configuration Options

Customize your playing range, note assignments, and CC mapping using command-line arguments:

### MPE MIDI Settings
*   `--base-note <int>`: MIDI note mapped to the leftmost edge of the screen (Default: `48`, which is C3).
*   `--octaves <float>`: Number of octaves spanning the total width of the screen (Default: `3.0`).
*   `--pb-range <int>`: MPE Pitch Bend Range in semitones. By default, this is automatically calculated as `12 * octaves` (e.g. `36` for 3 octaves, `48` for 4 octaves) to ensure continuous scaling.
*   `--velocity <int>`: Static strike velocity for triggered notes (Default: `100`).
*   `--midi-port <name>`: MIDI port to connect to. Set to `auto` (default) to prefer Virtual Raw MIDI, `virtual` to create a new software port, or specify a custom port name.

### Interaction Settings
*   `--freeze`: Stops the `xochitl` interface service on startup to lock the screen in place and prevent drawing/UI interactions. Restores it upon exit.
*   `--no-palm-rejection`: Disable palm rejection, enabling simultaneous touch (chords) + pen (solos) play.
*   `--timbre-cc <int>`: CC number sent on vertical (Y-axis) slide movement (Default: `74`).
*   `--pressure-type <aftertouch|cc11|cc1>`: MIDI message type for pressure modulation (Default: `aftertouch`).
*   `--tilt-x-cc <int>` / `--tilt-y-cc <int>`: CC numbers for physical pen tilt (Default: `16` and `17`).

### Device Overrides (Advanced)
*   `-r`, `--rotation <portrait|right|left|inverted>`: Orientation rotation of the tablet surface.
*   `--pen-device <path>` / `--touch-device <path>`: Manually specify event paths on the tablet (e.g., `/dev/input/event2`).
*   `--64bit` / `--32bit`: Force 64-bit (Paper Pro) or 32-bit (rM1/rM2) event structure parsing.

---

## License

Licensed under the [MIT License](LICENSE.md).
