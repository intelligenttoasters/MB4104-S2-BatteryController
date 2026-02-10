#!/usr/bin/env python3
# Battery Controller - UPS Terminal Dashboard
# Author: IntelligentToasters
# License: GNU General Public License v3.0
#
# Provides a lightweight terminal-based dashboard displaying real-time UPS
# status including network info, battery SOC, temperatures, inputs, runtime,
# and relay states. Supports interactive single-key commands for relay control.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
UPS CLI Dashboard

Provides a lightweight terminal-based dashboard that displays:
- Network information from CMD0 messages
- Selected UPS attributes from CMD10/CMD2 (SOC, temps, inputs, runtime, relays)

This script uses a non-blocking input thread to capture single-key commands (toggle relays, quit):
- On Windows: Uses msvcrt.kbhit() / msvcrt.getch() for non-blocking input
- On Unix/Linux/macOS: Uses termios/tty/select for single-key input in cbreak mode

It depends on `powertech_ups.UPSMonitor` to maintain the TCP connection, poll attributes,
and provide helpers like `get_all`, `get_attr`, `set_relays`, and `wait_for_update`.

Usage:
    scripts/ups_dashboard.py --host 192.168.1.1 --port 5555

Note:
- On Unix: Terminal handling uses cbreak mode; on exit we attempt to restore the original
  terminal attributes to avoid leaving your terminal in a broken state.
- On Windows: Standard terminal handling is used.
"""
from __future__ import annotations

import argparse
import time
import sys
import signal
import logging
import platform
import queue
import threading
from datetime import datetime

# Include the library path so we can import powertech_ups if this script is run from the project root
sys.path.append(".")

from powertech_ups import UPSMonitor, ATTR_NAMES

# Platform-specific imports
if platform.system() == 'Windows':
    import msvcrt
else:
    import termios
    import tty
    import select

log = logging.getLogger(__name__)

# Default attribute set shown in the dashboard (can be adjusted as needed)
DEFAULT_ATTRS = [3, 5, 6, 7, 8, 21, 22, 32]

# Human-friendly labels and units for attributes
LABELS = {
    1: "Output Relays",
    3: "State of Charge",
    5: "AC Load",
    6: "DC 12V30A Output",
    7: "DC USB-C Output",
    8: "DC USB-A Output",
    21: "Input (Solar+Grid)",
    22: "Grid Input",
    30: "Remaining Runtime",
    32: "Battery Temp",
    100: "Solar-only Input",
}

UNITS = {
    3: "%",
    5: "W",
    6: "W",
    7: "W",
    8: "W",
    21: "W",
    22: "W",
    30: "min",
    32: "°C",
    100: "W",
} 


def clear_screen():
    sys.stdout.write('\x1b[2J\x1b[H')


def format_val(aid, val):
    if val is None:
        return "-"
    if aid == 32:  # temp already in C
        return f"{val}{UNITS.get(aid, '')}"
    if aid == 3:
        return f"{val}{UNITS.get(aid, '')}"
    if isinstance(val, (int, float)):
        unit = UNITS.get(aid, '')
        return f"{val} {unit}".strip()
    # for dicts (e.g., relays)
    return str(val)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.1")
    parser.add_argument("--port", default=5555, type=int)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--simulate-file", default="data_result.txt")
    parser.add_argument("--interval", default=1.0, type=float, help="Max UI refresh interval in seconds")
    parser.add_argument("--polling", default=None, type=float, help="UPS polling interval in seconds (affects connection timeout)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose (DEBUG) logging output")
    args = parser.parse_args()

    # configure logging early according to --verbose flag
    logging.basicConfig(level=(logging.DEBUG if args.verbose else logging.INFO))
    # Silence package logs by default (be chatty only with --verbose)
    pkg_log = logging.getLogger('powertech_ups')
    pkg_log.setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    # Pass poll_interval into the monitor if provided
    monitor_kwargs = {}
    if args.polling is not None:
        monitor_kwargs['poll_interval'] = args.polling

    monitor = UPSMonitor(host=args.host, port=args.port, simulate=args.simulate, simulate_file=args.simulate_file if args.simulate else None, **monitor_kwargs)
    monitor.start()

    # Input handling thread (single-key commands)
    input_q: "queue.Queue[str]" = queue.Queue()
    input_stop = threading.Event()

    # capture original terminal state so we can restore on exit (Unix only)
    orig_termios = None
    fd = None
    if platform.system() != 'Windows':
        fd = sys.stdin.fileno()
        try:
            orig_termios = termios.tcgetattr(fd)
        except Exception:
            orig_termios = None

    def _restore_tty_sane(fd=None, orig=None):
        """Attempt to restore terminal to sane state (canonical + echo) - Unix only"""
        if platform.system() == 'Windows':
            return
        if fd is None:
            return
        try:
            if orig is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, orig)
                return
            attrs = termios.tcgetattr(fd)
            lflag = attrs[3]
            # Ensure canonical mode and echo are enabled
            lflag |= (termios.ICANON | termios.ECHO)
            attrs[3] = lflag
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass

    class InputThread(threading.Thread):
        """Background thread that collects single-key input without blocking the UI loop.

        Implementation notes:
        - On Unix/Linux/macOS:
          Uses `tty.setcbreak()` to enable cbreak mode so single characters are available
          immediately (no Enter required).
          Uses `select.select()` with a short timeout to poll stdin and allow clean shutdown.
        - On Windows:
          Uses `msvcrt.kbhit()` to poll for available input with a timeout loop.
          Character reading uses `msvcrt.getch()` for non-blocking input.
        
        On exit (finally block) the thread attempts to restore original terminal attributes
        (Unix only) to avoid leaving the terminal in a broken state.
        """
        def __init__(self, q, stop_evt, orig_termios):
            super().__init__(daemon=True)
            self.q = q
            self.stop_evt = stop_evt
            self.orig_termios = orig_termios

        def run(self):
            if platform.system() == 'Windows':
                self._run_windows()
            else:
                self._run_unix()

        def _run_windows(self):
            """Windows implementation using msvcrt"""
            try:
                while not self.stop_evt.is_set():
                    # msvcrt.kbhit() checks if a key press is waiting; poll with short sleep
                    if msvcrt.kbhit():
                        # Read the character without waiting
                        ch = msvcrt.getch().decode('utf-8', errors='ignore')
                        if ch:
                            self.q.put(ch)
                    else:
                        # Sleep briefly to avoid busy-waiting
                        time.sleep(0.2)
            except Exception:
                pass

        def _run_unix(self):
            """Unix implementation using termios/tty/select"""
            fd = sys.stdin.fileno()
            try:
                # Try to enable cbreak mode; if stdin is not a tty this will raise and we
                # simply run the loop using select reads (non-blocking behaviour remains).
                tty.setcbreak(fd)
                while not self.stop_evt.is_set():
                    r, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch:
                            # Push key to the queue for the main thread to handle
                            self.q.put(ch)
            finally:
                # Restore original terminal attrs if available. This is critical so the
                # user's terminal isn't left without echo or in non-canonical mode.
                if self.orig_termios is not None:
                    try:
                        termios.tcsetattr(fd, termios.TCSADRAIN, self.orig_termios)
                    except Exception:
                        pass
                else:
                    # If we don't have the original attributes, make a best-effort attempt
                    # to put the terminal back into canonical mode with echo enabled.
                    _restore_tty_sane(fd)

    input_thread = InputThread(input_q, input_stop, orig_termios)
    input_thread.start()

    last_cmd_status = None  # (msg, timestamp)

    def handle_exit(signum=None, frame=None):
        # Attempt to restore terminal to a sane state and exit
        if platform.system() != 'Windows':
            if orig_termios is not None and fd is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, orig_termios)
                except Exception:
                    pass
            else:
                _restore_tty_sane(fd, orig_termios)
        input_stop.set()
        monitor.stop()
        # allow some time for input thread to restore
        try:
            input_thread.join(timeout=0.5)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    # Also handle SIGTERM so we restore tty when the process is terminated by timeout
    try:
        signal.signal(signal.SIGTERM, handle_exit)
    except Exception:
        # some platforms may not support SIGTERM registration in this context
        pass

    help_text = "Controls: [1] toggle AC inverter, [2] toggle DC 12V10A, [3] toggle USB/High, [Q] quit"

    try:
        # Main UI loop: redraws the dashboard once per `args.interval` or whenever
        # `monitor.wait_for_update()` indicates new data arrived. The redraw is
        # intentionally simple and avoids complex terminal manipulations.
        while True:
            clear_screen()
            status = monitor.get_connection_status()
            cmd0 = monitor.get_cmd0_info()
            conn_text = "Connection: connected" if status.get("connected") else f"Connection: {status.get('last_error') or 'unable to connect'}"
            # connection status is shown in the Network section below (not on the title)
            print("UPS Dashboard")
            print("==============")

            # Network info (CMD0)
            if cmd0:
                print("\nNetwork:")
                for k in ("ip", "mac", "rssi", "sv", "hv"):
                    if k in cmd0:
                        print(f"  {k.upper():6}: {cmd0.get(k)}")
                # Show connection status directly below CMD0 data so it is in the
                # same logical section as the rest of the network information.
                print(f"  {conn_text}")
            elif status.get("connected"):
                # Connected but waiting for an initial CMD0 message from the device
                print("\nNetwork: (connected, waiting for CMD0 message)")
            else:
                print("\nNetwork: (waiting...)")

            # Snapshot the latest attributes; `get_all()` is inexpensive and threadsafe
            all_attrs = monitor.get_all()

            # UPS State group (SOC, temp, runtime)
            print("\nUPS State:")
            soc = format_val(3, all_attrs.get(3))
            temp = format_val(32, all_attrs.get(32))
            runtime = format_val(30, all_attrs.get(30))
            print(f"  {LABELS.get(3)}: {soc}")
            print(f"  {LABELS.get(32)}: {temp}")
            if runtime != "-":
                print(f"  Remaining runtime: {runtime}")

            # Input / Charging group
            print("\nInput / Charging:")
            in21 = format_val(21, all_attrs.get(21))
            in22 = format_val(22, all_attrs.get(22))
            solar_only = format_val(100, all_attrs.get(100))
            print(f"  {LABELS.get(21)}: {in21}")
            print(f"  {LABELS.get(22)}: {in22}")
            if solar_only != "-":
                print(f"  {LABELS.get(100)}: {solar_only}")

            # Outputs group (relays and loads)
            print("\nOutputs:")
            # Relays: monitor stores relay attribute as a dict with booleans and raw value
            relay = all_attrs.get(1)
            if isinstance(relay, dict):
                print("  Relays:")
                print(f"    AC inverter : {'ON' if relay.get('ac_inverter') else 'OFF'}")
                print(f"    DC 12V10A   : {'ON' if relay.get('dc_12v10a') else 'OFF'}")
                print(f"    USB/High    : {'ON' if relay.get('usb_highpower') else 'OFF'}")
            # Output loads
            print("  Output Loads:")
            print(f"    {LABELS.get(5)}: {format_val(5, all_attrs.get(5))}")
            print(f"    {LABELS.get(6)}: {format_val(6, all_attrs.get(6))}")
            print(f"    {LABELS.get(7)}: {format_val(7, all_attrs.get(7))}")
            print(f"    {LABELS.get(8)}: {format_val(8, all_attrs.get(8))}")

            # show last command status and controls below outputs
            if last_cmd_status is not None:
                msg, ts = last_cmd_status
                print(f"\nLast cmd: {msg} ({ts.strftime('%H:%M:%S')})")
            print(help_text)

            # process input keys (pulled from the InputThread queue)
            while not input_q.empty():
                k = input_q.get().lower()
                if k == 'q':
                    # graceful shutdown: signal the input thread, stop the monitor and exit
                    input_stop.set()
                    monitor.stop()
                    return
                if k in ('1','2','3'):
                    # Map keys to relay bits and compute desired toggle state using
                    # the monitor's cached attribute 1 (relay map). This keeps the UI
                    # responsive even if the device is slow to reply.
                    bit_map = {'1': 0x1, '2': 0x2, '3': 0x4}
                    bit = bit_map[k]
                    cur_raw = 0
                    cur_attr = monitor.get_attr(1)
                    if isinstance(cur_attr, dict):
                        cur_raw = int(cur_attr.get('raw', 0))
                    elif isinstance(cur_attr, int):
                        cur_raw = cur_attr
                    desired_on = not bool(cur_raw & bit)
                    values = bit if desired_on else 0
                    # set_relays implements a safety pre-check (CMD2) and will return
                    # False on failure so the UI can report the result to the user.
                    ok = monitor.set_relays(bit, values, timeout=2.0)
                    ts = datetime.now()
                    if ok:
                        last_cmd_status = (f"Toggled {k} -> {'ON' if desired_on else 'OFF'}", ts)
                    else:
                        last_cmd_status = (f"Failed to toggle {k}", ts)

            # Wait until either new data arrives or the interval elapses. This
            # makes the UI more responsive by redrawing immediately when the
            # monitor processes a new message (CMD0/CMD10/responses).
            try:
                fired = monitor.wait_for_update(timeout=args.interval)
                if fired:
                    # clear the 'update' flag to prepare for the next wait
                    monitor.clear_update()
            except Exception:
                # fall back to sleeping if the monitor does not support wait_for_update
                time.sleep(args.interval)
    finally:
        # restore terminal state and stop input thread
        input_stop.set()
        if platform.system() != 'Windows':
            if orig_termios is not None and fd is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, orig_termios)
                except Exception:
                    pass
            else:
                _restore_tty_sane(fd, orig_termios)
        try:
            input_thread.join(timeout=1.0)
        except Exception:
            pass
if __name__ == "__main__":
    main()
