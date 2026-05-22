#!/usr/bin/env python3
import argparse
import struct
import threading
import queue
import time
import sys
import paramiko
import mido

try:
    from rich.console import Console
    from rich.theme import Theme
    console = Console(theme=Theme({"info": "dim cyan", "warning": "magenta", "error": "bold red", "success": "bold green"}))
except ImportError:
    # Fallback to standard prints if rich is not available
    class SimpleConsole:
        def print(self, *args, style=None, **kwargs):
            print(*args, **kwargs)
    console = SimpleConsole()

EVENT_FORMAT_64 = '<qqHHi'
EVENT_SIZE_64 = 24

EVENT_FORMAT_32 = '<iiHHi'
EVENT_SIZE_32 = 16

def read_events(ssh_client, device_path, q, is_64bit):
    fmt = EVENT_FORMAT_64 if is_64bit else EVENT_FORMAT_32
    size = EVENT_SIZE_64 if is_64bit else EVENT_SIZE_32
    
    transport = ssh_client.get_transport()
    channel = transport.open_session()
    channel.exec_command(f"cat {device_path}")
    
    buffer = b""
    while True:
        try:
            chunk = channel.recv(4096)
            if not chunk:
                break
            buffer += chunk
            
            while len(buffer) >= size:
                event_bytes = buffer[:size]
                buffer = buffer[size:]
                
                tv_sec, tv_usec, e_type, e_code, e_value = struct.unpack(fmt, event_bytes)
                
                q.put({
                    'device': device_path,
                    'time': tv_sec + tv_usec / 1e6,
                    'type': e_type,
                    'code': e_code,
                    'value': e_value
                })
        except Exception as e:
            console.print(f"[error]Error reading from {device_path}: {e}[/error]")
            break

def apply_rotation(x, y, rotation, max_x_p, max_y_p):
    if rotation == 'right':
        return max_y_p - y, x
    elif rotation == 'left':
        return y, max_x_p - x
    elif rotation == 'inverted':
        return max_x_p - x, max_y_p - y
    return x, y

class MPEChannelAllocator:
    """Manages dynamic allocation of MIDI Channels 2 to 16 for polyphonic voice isolation."""
    def __init__(self, min_channel=1, max_channel=15): # 0-indexed: Channel 2 (1) to Channel 16 (15)
        self.min_channel = min_channel
        self.max_channel = max_channel
        self.active_allocations = {} # key -> channel
        self.channel_notes = {c: None for c in range(min_channel, max_channel + 1)}
        self.channel_key = {c: None for c in range(min_channel, max_channel + 1)}
        self.round_robin_index = min_channel

    def allocate(self, key):
        if key in self.active_allocations:
            return self.active_allocations[key]
        
        # Find a free channel
        for offset in range(self.max_channel - self.min_channel + 1):
            c = self.min_channel + (self.round_robin_index - self.min_channel + offset) % (self.max_channel - self.min_channel + 1)
            if self.channel_key[c] is None:
                self.channel_key[c] = key
                self.active_allocations[key] = c
                self.round_robin_index = self.min_channel + (c - self.min_channel + 1) % (self.max_channel - self.min_channel + 1)
                return c
                
        # Channel stealing fallback
        c = self.round_robin_index
        old_key = self.channel_key[c]
        if old_key is not None:
            self.active_allocations.pop(old_key, None)
        self.channel_key[c] = key
        self.active_allocations[key] = c
        self.round_robin_index = self.min_channel + (c - self.min_channel + 1) % (self.max_channel - self.min_channel + 1)
        return c

    def release(self, key):
        if key in self.active_allocations:
            c = self.active_allocations.pop(key)
            self.channel_key[c] = None
            return c
        return None

class TabletMPEDriver:
    def __init__(self, midi_port, pen_max_x=11180, pen_max_y=15340, touch_max_x=2049, touch_max_y=2814,
                 rotation='portrait', palm_rejection=True, base_note=36, octaves=4.0, pb_range=48,
                 velocity=100, timbre_cc=74, pressure_cc='aftertouch', tilt_x_cc=16, tilt_y_cc=17,
                 verbose=False):
        
        self.port = midi_port
        self.pen_max_x_p = pen_max_x
        self.pen_max_y_p = pen_max_y
        self.touch_max_x_p = touch_max_x
        self.touch_max_y_p = touch_max_y
        self.rotation = rotation
        self.palm_rejection = palm_rejection
        self.verbose = verbose
        
        self.base_note = base_note
        self.octaves = octaves
        self.pb_range = pb_range
        self.velocity = velocity
        self.timbre_cc = timbre_cc
        self.pressure_cc = pressure_cc
        self.tilt_x_cc = tilt_x_cc
        self.tilt_y_cc = tilt_y_cc
        
        if rotation in ['right', 'left']:
            pen_max_x, pen_max_y = pen_max_y, pen_max_x
            touch_max_x, touch_max_y = touch_max_y, touch_max_x
            
        self.pen_max_x = pen_max_x
        self.pen_max_y = pen_max_y
        self.touch_max_x = touch_max_x
        self.touch_max_y = touch_max_y

        # State tracking
        self.allocator = MPEChannelAllocator()
        
        # Pen State
        self.pending_pen = -1
        self.pending_rubber = -1
        self.emitted_pen = 0
        self.emitted_rubber = 0
        self.pen_touch = 0
        self.pen_raw_x = 0
        self.pen_raw_y = 0
        self.pen_pressure = 0
        self.pen_tilt_x = 0
        self.pen_tilt_y = 0
        self.pen_dirty = False
        self.pen_was_touching = False
        
        # Touch State
        self.current_slot = 0
        self.mt_slots = {}
        self.mt_positions = {}
        self.mt_touching = {} # slot -> bool (active contact)
        self.touch_frame_slots = {}

    def send_midi(self, msg):
        self.port.send(msg)

    def process_pen_event(self, ev):
        e_type, e_code, e_value = ev['type'], ev['code'], ev['value']

        if e_type == 1: # EV_KEY
            if e_code == 320: # BTN_TOOL_PEN
                self.pending_pen = e_value
                return
            elif e_code == 321: # BTN_TOOL_RUBBER
                self.pending_rubber = e_value
                return
            elif e_code == 330: # BTN_TOUCH
                self.pen_touch = e_value
                self.pen_dirty = True
                return

        if e_type == 3: # EV_ABS
            if e_code == 25: # ABS_DISTANCE
                return
            elif e_code == 26: # ABS_TILT_X -> Swapped with Y in hardware correction
                self.pen_tilt_y = e_value
                self.pen_dirty = True
                return
            elif e_code == 27: # ABS_TILT_Y -> Swapped with X in hardware correction
                self.pen_tilt_x = e_value
                self.pen_dirty = True
                return
            elif e_code == 0: # ABS_X
                self.pen_raw_x = e_value
                self.pen_dirty = True
                return
            elif e_code == 1: # ABS_Y
                self.pen_raw_y = e_value
                self.pen_dirty = True
                return
            elif e_code == 24: # ABS_PRESSURE
                self.pen_pressure = e_value
                self.pen_dirty = True
                return

        if e_type == 0 and e_code == 0: # EV_SYN SYN_REPORT
            self.sync_pen_frame()

    def sync_pen_frame(self):
        # Update pen hovering/proximity status
        if self.pending_pen != -1:
            self.emitted_pen = self.pending_pen
            self.pending_pen = -1
        if self.pending_rubber != -1:
            self.emitted_rubber = self.pending_rubber
            self.pending_rubber = -1

        pen_present = (self.emitted_pen == 1 or self.emitted_rubber == 1)

        # Palm rejection: cancel touch notes instantly if enabled and pen is close
        if self.palm_rejection and pen_present:
            self.cancel_all_touches()

        if not self.pen_dirty:
            return
        self.pen_dirty = False

        # Physical touch transition
        is_touching = (self.pen_touch == 1)
        
        if is_touching and not self.pen_was_touching:
            # Note On Trigger
            ch = self.allocator.allocate("pen")
            rot_x, rot_y = apply_rotation(self.pen_raw_x, self.pen_raw_y, self.rotation, self.pen_max_x_p, self.pen_max_y_p)
            
            # Map X to fractional note
            x_norm = max(0.0, min(1.0, rot_x / self.pen_max_x))
            frac_note = self.base_note + x_norm * (12.0 * self.octaves)
            int_note = int(round(frac_note))
            self.allocator.channel_notes[ch] = int_note
            
            # 1. Pitch Bend
            offset = frac_note - int_note
            pb_val = int(offset * 8192.0 / self.pb_range)
            pb_val = max(-8192, min(8191, pb_val))
            self.send_midi(mido.Message('pitchwheel', pitch=pb_val, channel=ch))
            
            # 2. Timbre (Y axis) - Normalized so top is 127 and bottom is 0
            y_norm = max(0.0, min(1.0, rot_y / self.pen_max_y))
            timbre_val = int((1.0 - y_norm) * 127)
            self.send_midi(mido.Message('control_change', control=self.timbre_cc, value=timbre_val, channel=ch))
            
            # 3. Pressure
            pressure_val = int((self.pen_pressure / 4095.0) * 127)
            if self.pressure_cc == 'aftertouch':
                self.send_midi(mido.Message('aftertouch', value=pressure_val, channel=ch))
            else:
                self.send_midi(mido.Message('control_change', control=self.pressure_cc, value=pressure_val, channel=ch))
                
            # 4. Note On
            self.send_midi(mido.Message('note_on', note=int_note, velocity=self.velocity, channel=ch))
            
            console.print(f"[success]Pen Note On:[/success] Channel {ch+1}, Note {int_note} (Freq offset: {offset:+.2f} semitones)")
            
        elif is_touching and self.pen_was_touching:
            # Continuous modulation while dragging
            ch = self.allocator.allocate("pen")
            int_note = self.allocator.channel_notes.get(ch)
            
            if int_note is not None:
                rot_x, rot_y = apply_rotation(self.pen_raw_x, self.pen_raw_y, self.rotation, self.pen_max_x_p, self.pen_max_y_p)
                
                # Pitch Bend
                x_norm = max(0.0, min(1.0, rot_x / self.pen_max_x))
                frac_note = self.base_note + x_norm * (12.0 * self.octaves)
                offset = frac_note - int_note
                pb_val = int(offset * 8192.0 / self.pb_range)
                pb_val = max(-8192, min(8191, pb_val))
                self.send_midi(mido.Message('pitchwheel', pitch=pb_val, channel=ch))
                
                # Timbre Y
                y_norm = max(0.0, min(1.0, rot_y / self.pen_max_y))
                timbre_val = int((1.0 - y_norm) * 127)
                self.send_midi(mido.Message('control_change', control=self.timbre_cc, value=timbre_val, channel=ch))
                
                # Pressure
                pressure_val = int((self.pen_pressure / 4095.0) * 127)
                if self.pressure_cc == 'aftertouch':
                    self.send_midi(mido.Message('aftertouch', value=pressure_val, channel=ch))
                else:
                    self.send_midi(mido.Message('control_change', control=self.pressure_cc, value=pressure_val, channel=ch))
                
                # Tilt X/Y mapped to CCs
                # raw tilt values go from ~ -9000 to 9000
                tilt_x_val = int(((self.pen_tilt_x + 9000) / 18000.0) * 127)
                tilt_y_val = int(((self.pen_tilt_y + 9000) / 18000.0) * 127)
                self.send_midi(mido.Message('control_change', control=self.tilt_x_cc, value=max(0, min(127, tilt_x_val)), channel=ch))
                self.send_midi(mido.Message('control_change', control=self.tilt_y_cc, value=max(0, min(127, tilt_y_val)), channel=ch))
                
        elif not is_touching and self.pen_was_touching:
            # Note Off Trigger
            ch = self.allocator.release("pen")
            if ch is not None:
                int_note = self.allocator.channel_notes[ch]
                if int_note is not None:
                    self.send_midi(mido.Message('note_off', note=int_note, velocity=0, channel=ch))
                    self.send_midi(mido.Message('pitchwheel', pitch=0, channel=ch))
                self.allocator.channel_notes[ch] = None
                console.print(f"[warning]Pen Note Off:[/warning] Channel {ch+1}, Note {int_note}")

        self.pen_was_touching = is_touching

    def process_touch_event(self, ev):
        e_type, e_code, e_value = ev['type'], ev['code'], ev['value']

        if e_type == 3: # EV_ABS
            if e_code == 47: # ABS_MT_SLOT
                self.current_slot = e_value
                return
                
        if self.current_slot not in self.touch_frame_slots:
            self.touch_frame_slots[self.current_slot] = {}

        if e_type == 3: # EV_ABS
            if e_code == 57: # ABS_MT_TRACKING_ID
                self.touch_frame_slots[self.current_slot]['tracking_id'] = e_value
                if e_value == -1:
                    self.mt_slots.pop(self.current_slot, None)
                    self.mt_positions.pop(self.current_slot, None)
                    self.mt_touching[self.current_slot] = False
                else:
                    self.mt_slots[self.current_slot] = e_value
                    self.mt_touching[self.current_slot] = True
                    if self.current_slot not in self.mt_positions: 
                        self.mt_positions[self.current_slot] = {"raw_x": 0, "raw_y": 0, "pressure": 64}
            elif e_code == 53: # ABS_MT_POSITION_X
                if self.current_slot not in self.mt_positions: 
                    self.mt_positions[self.current_slot] = {"raw_x": 0, "raw_y": 0, "pressure": 64}
                self.mt_positions[self.current_slot]["raw_x"] = e_value
                self.touch_frame_slots[self.current_slot]['x'] = True
            elif e_code == 54: # ABS_MT_POSITION_Y
                if self.current_slot not in self.mt_positions: 
                    self.mt_positions[self.current_slot] = {"raw_x": 0, "raw_y": 0, "pressure": 64}
                self.mt_positions[self.current_slot]["raw_y"] = e_value
                self.touch_frame_slots[self.current_slot]['y'] = True
            elif e_code == 48: # ABS_MT_TOUCH_MAJOR
                if self.current_slot not in self.mt_positions: 
                    self.mt_positions[self.current_slot] = {"raw_x": 0, "raw_y": 0, "pressure": 64}
                # Normalize touch size/pressure (8 to 21 range) to midi 0-127
                self.mt_positions[self.current_slot]["pressure"] = max(0, min(127, int((e_value - 8) / 13.0 * 127)))
                self.touch_frame_slots[self.current_slot]['pressure'] = True

        if e_type == 0 and e_code == 0: # EV_SYN SYN_REPORT
            self.sync_touch_frame()

    def sync_touch_frame(self):
        pen_present = (self.emitted_pen == 1 or self.emitted_rubber == 1)
        if self.palm_rejection and pen_present:
            self.touch_frame_slots = {}
            return

        for slot, updates in self.touch_frame_slots.items():
            key = f"touch_{slot}"
            
            # Touch Lifted (Note Off)
            if 'tracking_id' in updates and updates['tracking_id'] == -1:
                ch = self.allocator.release(key)
                if ch is not None:
                    int_note = self.allocator.channel_notes[ch]
                    if int_note is not None:
                        self.send_midi(mido.Message('note_off', note=int_note, velocity=0, channel=ch))
                        self.send_midi(mido.Message('pitchwheel', pitch=0, channel=ch))
                    self.allocator.channel_notes[ch] = None
                    console.print(f"[info]Touch Lifted Slot {slot}:[/info] Channel {ch+1}, Note {int_note}")
                continue

            pos = self.mt_positions.get(slot)
            if pos is None:
                continue

            rot_x, rot_y = apply_rotation(pos["raw_x"], pos["raw_y"], self.rotation, self.touch_max_x_p, self.touch_max_y_p)
            
            # Calculate fractional pitch
            x_norm = max(0.0, min(1.0, rot_x / self.touch_max_x))
            frac_note = self.base_note + x_norm * (12.0 * self.octaves)
            
            # Touch Struck (Note On)
            if 'tracking_id' in updates and updates['tracking_id'] != -1:
                ch = self.allocator.allocate(key)
                int_note = int(round(frac_note))
                self.allocator.channel_notes[ch] = int_note
                
                # Pitch Bend
                offset = frac_note - int_note
                pb_val = int(offset * 8192.0 / self.pb_range)
                pb_val = max(-8192, min(8191, pb_val))
                self.send_midi(mido.Message('pitchwheel', pitch=pb_val, channel=ch))
                
                # Timbre Y
                y_norm = max(0.0, min(1.0, rot_y / self.touch_max_y))
                timbre_val = int((1.0 - y_norm) * 127)
                self.send_midi(mido.Message('control_change', control=self.timbre_cc, value=timbre_val, channel=ch))
                
                # Pressure
                pressure_val = pos["pressure"]
                if self.pressure_cc == 'aftertouch':
                    self.send_midi(mido.Message('aftertouch', value=pressure_val, channel=ch))
                else:
                    self.send_midi(mido.Message('control_change', control=self.pressure_cc, value=pressure_val, channel=ch))
                
                # Note On
                self.send_midi(mido.Message('note_on', note=int_note, velocity=self.velocity, channel=ch))
                console.print(f"[success]Touch Strike Slot {slot}:[/success] Channel {ch+1}, Note {int_note} (Freq offset: {offset:+.2f} semitones)")
                
            # Touch Moved (Modulation)
            elif 'x' in updates or 'y' in updates or 'pressure' in updates:
                ch = self.allocator.allocate(key)
                int_note = self.allocator.channel_notes.get(ch)
                
                if int_note is not None:
                    # Pitch Bend
                    offset = frac_note - int_note
                    pb_val = int(offset * 8192.0 / self.pb_range)
                    pb_val = max(-8192, min(8191, pb_val))
                    self.send_midi(mido.Message('pitchwheel', pitch=pb_val, channel=ch))
                    if self.verbose:
                        console.print(f"[info]Touch Moved Slot {slot}:[/info] Channel {ch+1}, Pitch Bend: {pb_val}")
                    
                    # Timbre Y
                    y_norm = max(0.0, min(1.0, rot_y / self.touch_max_y))
                    timbre_val = int((1.0 - y_norm) * 127)
                    self.send_midi(mido.Message('control_change', control=self.timbre_cc, value=timbre_val, channel=ch))
                    
                    # Pressure
                    pressure_val = pos["pressure"]
                    if self.pressure_cc == 'aftertouch':
                        self.send_midi(mido.Message('aftertouch', value=pressure_val, channel=ch))
                    else:
                        self.send_midi(mido.Message('control_change', control=self.pressure_cc, value=pressure_val, channel=ch))

        self.touch_frame_slots = {}

    def cancel_all_touches(self):
        for slot in list(self.mt_slots.keys()):
            key = f"touch_{slot}"
            ch = self.allocator.release(key)
            if ch is not None:
                int_note = self.allocator.channel_notes[ch]
                if int_note is not None:
                    self.send_midi(mido.Message('note_off', note=int_note, velocity=0, channel=ch))
                    self.send_midi(mido.Message('pitchwheel', pitch=0, channel=ch))
                self.allocator.channel_notes[ch] = None
                console.print(f"[warning]Palm Cancelled Touch Slot {slot}[/warning]")
        self.mt_slots.clear()
        self.mt_positions.clear()
        self.mt_touching.clear()

    def cleanup(self):
        # Shut off any hanging notes before closing
        console.print("[info]Shutting off active MIDI notes...[/info]")
        self.cancel_all_touches()
        ch = self.allocator.release("pen")
        if ch is not None:
            int_note = self.allocator.channel_notes[ch]
            if int_note is not None:
                self.send_midi(mido.Message('note_off', note=int_note, velocity=0, channel=ch))
                self.send_midi(mido.Message('pitchwheel', pitch=0, channel=ch))
            self.allocator.channel_notes[ch] = None
        # Send All Notes Off on all channels
        for ch in range(16):
            self.send_midi(mido.Message('control_change', control=123, value=0, channel=ch))
            self.send_midi(mido.Message('pitchwheel', pitch=0, channel=ch))

def send_mpe_init_messages(port, num_channels=15, pb_range=48):
    # Channel 1 (0-indexed 0) is the master channel for lower zone
    master_ch = 0
    # MPE Zone Configuration RPN (RPN 6)
    port.send(mido.Message('control_change', channel=master_ch, control=101, value=0))
    port.send(mido.Message('control_change', channel=master_ch, control=100, value=6))
    port.send(mido.Message('control_change', channel=master_ch, control=6, value=num_channels))
    
    # Pitch Bend Sensitivity RPN (RPN 0) to set pitch bend range for the zone
    for ch in range(16):
        port.send(mido.Message('control_change', channel=ch, control=101, value=0))
        port.send(mido.Message('control_change', channel=ch, control=100, value=0))
        port.send(mido.Message('control_change', channel=ch, control=6, value=pb_range))

def main():
    parser = argparse.ArgumentParser(description="reMarkable MPE MIDI Controller Driver")
    parser.add_argument("--host", default="10.11.99.1", help="Tablet IP address")
    parser.add_argument("--key", default="", help="Path to SSH private key")
    parser.add_argument("-r", "--rotation", choices=["portrait", "right", "left", "inverted"], default="portrait", help="Tablet physical orientation")
    
    # MPE Specific Settings
    parser.add_argument("--base-note", type=int, default=48, help="MIDI note mapped to leftmost screen edge (Default: 48, C3)")
    parser.add_argument("--octaves", type=float, default=3.0, help="Number of octaves spanning screen width (Default: 3.0)")
    parser.add_argument("--pb-range", type=int, default=None, help="MPE Pitch Bend Range in semitones (Default: 12 * octaves)")
    parser.add_argument("--velocity", type=int, default=100, help="Default Note On strike velocity (Default: 100)")
    
    # Palm rejection toggles
    palm_group = parser.add_mutually_exclusive_group()
    palm_group.add_argument("--palm-rejection", dest="palm_rejection", action="store_true", default=True, help="Enable touch cancellation when pen is in proximity (Default)")
    palm_group.add_argument("--no-palm-rejection", dest="palm_rejection", action="store_false", help="Disable palm rejection, allowing simultaneous stylus + touch interaction")
    
    # CC Mapping Config
    parser.add_argument("--timbre-cc", type=int, default=74, help="CC number for Y-axis vertical slide (Default: 74)")
    parser.add_argument("--pressure-type", choices=["aftertouch", "cc11", "cc1"], default="aftertouch", help="MIDI message type to represent pressure (Default: aftertouch)")
    parser.add_argument("--tilt-x-cc", type=int, default=16, help="CC number for pen Tilt X (Default: 16)")
    parser.add_argument("--tilt-y-cc", type=int, default=17, help="CC number for pen Tilt Y (Default: 17)")
    parser.add_argument("--freeze", action="store_true", help="Freeze screen (stop xochitl) while playing to prevent UI interactions")

    # Digitizer and Event Stream overrides
    parser.add_argument("--pen-device", default=None, help="Override pen event path (e.g. /dev/input/event2)")
    parser.add_argument("--touch-device", default=None, help="Override touch event path (e.g. /dev/input/event3)")
    parser.add_argument("--pen-max-x", type=int, default=None, help="Override pen max X coordinate")
    parser.add_argument("--pen-max-y", type=int, default=None, help="Override pen max Y coordinate")
    parser.add_argument("--touch-max-x", type=int, default=None, help="Override touch max X coordinate")
    parser.add_argument("--touch-max-y", type=int, default=None, help="Override touch max Y coordinate")
    
    struct_group = parser.add_mutually_exclusive_group()
    struct_group.add_argument("--64bit", dest="is_64bit", action="store_true", default=None, help="Force 64-bit event struct format (Paper Pro, Move)")
    struct_group.add_argument("--32bit", dest="is_64bit", action="store_false", default=None, help="Force 32-bit event struct format (rM1, rM2)")
    
    parser.add_argument("--no-pen", action="store_true", help="Disable pen input capturing")
    parser.add_argument("--no-touch", action="store_true", help="Disable touch input capturing")
    parser.add_argument("--midi-port", default="auto", help="MIDI port to connect to ('auto', 'virtual', or port name). Default: auto")
    parser.add_argument("--verbose", action="store_true", help="Print verbose MIDI event logs")
    args = parser.parse_args()

    if args.no_pen and args.no_touch:
        console.print("[error]Error: Both pen and touch inputs are disabled. Nothing to do![/error]")
        sys.exit(1)

    pb_range = args.pb_range if args.pb_range is not None else int(round(args.octaves * 12))

    # Initialize MIDI Port
    try:
        # Force rtmidi backend
        mido.set_backend('mido.backends.rtmidi')
        if args.midi_port.lower() == 'virtual':
            console.print("Starting virtual MIDI Port 'reMarkable MPE'...")
            midi_port = mido.open_output("reMarkable MPE", virtual=True)
            console.print("[success]Virtual MIDI Port successfully registered![/success]")
        elif args.midi_port.lower() == 'auto':
            available_ports = mido.get_output_names()
            # 1. Prefer Virtual Raw MIDI (hardware simulation)
            target_port = next((p for p in available_ports if 'virtual raw midi' in p.lower()), None)
            # 2. Fallback to Midi Through
            if not target_port:
                target_port = next((p for p in available_ports if 'midi through' in p.lower()), None)
                
            if target_port:
                console.print(f"Auto-selected MIDI port: {target_port}")
                midi_port = mido.open_output(target_port)
                console.print(f"[success]Successfully connected to MIDI port: {target_port}[/success]")
            else:
                console.print("[error]Auto-selection failed. No 'Virtual Raw MIDI' or 'Midi Through' ports found.[/error]")
                sys.exit(1)
        else:
            console.print(f"Connecting to MIDI port matching '{args.midi_port}'...")
            available_ports = mido.get_output_names()
            target_port = next((p for p in available_ports if args.midi_port.lower() in p.lower()), None)
            
            if target_port:
                midi_port = mido.open_output(target_port)
                console.print(f"[success]Successfully connected to MIDI port: {target_port}[/success]")
            else:
                console.print(f"[error]Could not find MIDI port matching '{args.midi_port}'.[/error]")
                console.print(f"Available ports: {available_ports}")
                sys.exit(1)
    except Exception as e:
        console.print(f"[error]Failed to open MIDI port: {e}[/error]")
        sys.exit(1)

    # Send MPE Init SysEx/RPNs
    send_mpe_init_messages(midi_port, num_channels=15, pb_range=pb_range)

    # Setup SSH Connection
    console.print(f"Connecting to {args.host} over SSH...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        if args.key:
            ssh.connect(args.host, username='root', key_filename=args.key)
        else:
            ssh.connect(args.host, username='root')
    except Exception as e:
        console.print(f"[error]Failed to connect: {e}[/error]")
        midi_port.close()
        sys.exit(1)
        
    console.print("[success]Connected to tablet![/success]")
    
    if args.freeze:
        console.print("[info]Freezing tablet screen (stopping xochitl)...[/info]")
        ssh.exec_command("systemctl stop xochitl")
    
    # Auto-detect tablet model
    stdin, stdout, stderr = ssh.exec_command("cat /sys/devices/soc0/machine")
    model = stdout.read().decode('utf-8').strip()
    console.print(f"Detected Model: [success]{model}[/success]")
    
    # Baseline defaults based on model
    if model in ["reMarkable Ferrari", "reMarkable Pro"]:
        detected_is_64bit = True
        detected_pen_device = "/dev/input/event2"
        detected_touch_device = "/dev/input/event3"
        detected_pen_max_x, detected_pen_max_y = 11180, 15340
        detected_touch_max_x, detected_touch_max_y = 2049, 2814
    elif "Move" in model:
        detected_is_64bit = True
        detected_pen_device = "/dev/input/event2"
        detected_touch_device = "/dev/input/event3"
        detected_pen_max_x, detected_pen_max_y = 6678, 11872
        detected_touch_max_x, detected_touch_max_y = 1200, 2200
    elif model == "reMarkable 2.0":
        detected_is_64bit = False
        detected_pen_device = "/dev/input/event1"
        detected_touch_device = "/dev/input/event2"
        detected_pen_max_x, detected_pen_max_y = 20967, 15725
        detected_touch_max_x, detected_touch_max_y = 767, 1023
    else: # reMarkable 1.0 or unknown
        detected_is_64bit = False
        detected_pen_device = "/dev/input/event0"
        detected_touch_device = "/dev/input/event1"
        detected_pen_max_x, detected_pen_max_y = 20967, 15725
        detected_touch_max_x, detected_touch_max_y = 767, 1023

    # Apply overrides
    is_64bit = args.is_64bit if args.is_64bit is not None else detected_is_64bit
    pen_device = args.pen_device if args.pen_device is not None else detected_pen_device
    touch_device = args.touch_device if args.touch_device is not None else detected_touch_device
    pen_max_x = args.pen_max_x if args.pen_max_x is not None else detected_pen_max_x
    pen_max_y = args.pen_max_y if args.pen_max_y is not None else detected_pen_max_y
    touch_max_x = args.touch_max_x if args.touch_max_x is not None else detected_touch_max_x
    touch_max_y = args.touch_max_y if args.touch_max_y is not None else detected_touch_max_y

    pressure_cc = 'aftertouch' if args.pressure_type == 'aftertouch' else (11 if args.pressure_type == 'cc11' else 1)

    driver = TabletMPEDriver(
        midi_port=midi_port,
        pen_max_x=pen_max_x, pen_max_y=pen_max_y,
        touch_max_x=touch_max_x, touch_max_y=touch_max_y,
        rotation=args.rotation,
        palm_rejection=args.palm_rejection,
        base_note=args.base_note,
        octaves=args.octaves,
        pb_range=pb_range,
        velocity=args.velocity,
        timbre_cc=args.timbre_cc,
        pressure_cc=pressure_cc,
        tilt_x_cc=args.tilt_x_cc,
        tilt_y_cc=args.tilt_y_cc,
        verbose=args.verbose
    )

    q = queue.Queue()
    threads = []
    devices = []
    
    if not args.no_pen:
        devices.append(pen_device)
    if not args.no_touch:
        devices.append(touch_device)
        
    for dev in devices:
        t = threading.Thread(target=read_events, args=(ssh, dev, q, is_64bit), daemon=True)
        t.start()
        threads.append(t)

    console.print(f"\n[success]reMarkable MPE Controller is active![/success]")
    console.print(f"Base Note: {args.base_note} | Range: {args.octaves} Octaves | Pitch Bend Range: {pb_range} semitones")
    console.print(f"Palm Rejection: {'[success]Enabled[/success]' if args.palm_rejection else '[warning]Disabled[/warning]'}")
    console.print("(Press Ctrl+C to exit and clean up notes)\n")

    try:
        while True:
            ev = q.get()
            if not args.no_pen and ev['device'] == pen_device:
                driver.process_pen_event(ev)
            elif not args.no_touch and ev['device'] == touch_device:
                driver.process_touch_event(ev)
    except KeyboardInterrupt:
        console.print("\n[warning]Shutting down driver...[/warning]")
    finally:
        driver.cleanup()
        midi_port.close()
        if args.freeze:
            console.print("[info]Unfreezing tablet screen (starting xochitl)...[/info]")
            try:
                stdin, stdout, stderr = ssh.exec_command("systemctl start xochitl")
                stdout.read()  # block until command completes
            except Exception as e:
                console.print(f"[error]Failed to restart xochitl: {e}[/error]")
        ssh.close()
        console.print("[success]Virtual MIDI Port closed and SSH disconnected.[/success]")

if __name__ == "__main__":
    main()
