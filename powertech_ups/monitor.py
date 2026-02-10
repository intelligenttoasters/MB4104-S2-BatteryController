# Battery Controller - UPS Monitor Library
# Author: IntelligentToasters
# License: GNU General Public License v3.0
#
# Provides the UPSMonitor class: a background TCP monitor that maintains a
# persistent connection to UPS devices, parses newline-delimited JSON messages,
# and offers thread-safe helpers for querying attributes and controlling relays.
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

"""powertech_ups.monitor

UPSMonitor: a background TCP monitor that keeps the most recent values
reported by a UPS-like device and provides safe helpers for querying and
controlling the device.

High-level responsibilities
- Maintain a single persistent TCP connection to the device, reading
  newline-delimited JSON messages and parsing them into internal state.
- Expose thread-safe getters for attributes and derived values.
- Provide a synchronous `send_command` helper that sends JSON commands
  (CMD2, CMD3, CMD0, etc.) and waits for matching responses via `sn`.
- Run a lightweight background poller that actively requests attribute
  groups using CMD2 (fast and slow groups) so the UI sees updated values
  even when the device doesn't proactively push them.

Primary types / functions
- class UPSMonitor
    - start/stop: manage background threads
    - _connect_and_read: connection and reader loop (where messages are
      parsed and dispatched)
    - _handle_msg: central message handler for CMD0, CMD2, CMD10 and command
      responses (matching by SN)
    - get_attrs(attrs, timeout): explicit CMD2 request and parse helper
    - set_relays(mask, values, timeout): safely set relay bits (CMD3) with
      a pre-check via CMD2 to avoid acting on stale/invalid states
    - send_command(cmd, payload, timeout): send JSON and wait for response

Design notes and thread safety
- A single persistent socket is used for all reads/writes; `send_command`
  uses an `outstanding` mapping (sn -> Event/storage) to match responses
  delivered by the reader thread.
- Access to mutable fields such as `_attrs` and the socket is guarded by
  `self._lock` (an RLock) where necessary; the poller and UI interact with
  the monitor via the public methods which are thread-safe.

"""
from __future__ import annotations

import socket
import threading
import time
import json
import logging
from typing import Optional, Dict, Any

from .parser import parse_line, process_cmd10

log = logging.getLogger(__name__)


class UPSMonitor:
    """Monitor a UPS-like device over TCP and keep latest data.

    - Connects to host:port and reads newline-delimited JSON messages.
    - CMD0 is processed once and cached.
    - CMD10 attributes are parsed and stored with a timestamp.
    - Thread-safe getters are provided.
    - Supports send_command() which waits for responses matched by `sn`.
    """

    def __init__(self, host: str = "192.168.11.5", port: int = 5555, simulate: bool = False, simulate_file: Optional[str] = None, poll_interval: float = 1.0):
        # Public configuration
        self.host = host
        self.port = port

        # Socket used for the persistent connection. It is created and owned
        # by the background thread in `_connect_and_read` and should only be
        # accessed while holding `self._lock` to avoid races during reconnects.
        self._sock: Optional[socket.socket] = None

        # Re-entrant lock for protecting connection-socket and attribute state
        # The RLock allows a caller to acquire the lock multiple times if the
        # call stack requires it.
        self._lock = threading.RLock()

        # Thread & lifecycle controls
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Polling interval controls how often we actively request CMD2 fast group.
        # Also influences the connection inactivity timeout: connection_timeout = poll_interval * 10
        self._poll_interval = float(poll_interval)
        if self._poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        # last message timestamp (used to detect idle connections)
        self._last_msg_ts: Optional[float] = None

        # state
        self._cmd0_info: Optional[Dict[str, Any]] = None
        self._attrs: Dict[int, Dict[str, Any]] = {}  # attr_id -> {value, ts}
        # connection state
        self._connected: bool = False
        self._last_error: Optional[str] = None

        # outstanding requests by sn -> (Event, result)
        self._outstanding: Dict[str, Any] = {}
        self._outstanding_lock = threading.Lock()

        # simulate mode for testing using a file of messages
        self.simulate = simulate
        self.simulate_file = simulate_file

        # Event set whenever new data arrives (CMD0/CMD10 or command response).
        # Dashboard/UI can wait on this to update immediately when new messages are processed.
        self._update_event = threading.Event()

        # Poller controls: poll groups in the background to actively request attributes
        # Group1 (slow-changing): attrs [3, 32] requested every 10 seconds (every 10 polls)
        # Group2 (fast): attrs [1,5,6,7,8,21,22,30] requested once per second (configurable via poll_interval)
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._poll_stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ups-monitor")
        self._thread.start()
        # start poller thread which will active request CMD2 groups when connected
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="ups-poller")
        self._poll_thread.start()
        log.debug("UPSMonitor thread started")

    def stop(self) -> None:
        self._stop_event.set()
        # stop poller
        try:
            self._poll_stop_event.set()
        except Exception:
            pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._poll_thread:
            try:
                self._poll_thread.join(timeout=2.0)
            except Exception:
                pass
        log.debug("UPSMonitor stopped")

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                if self.simulate and self.simulate_file:
                    self._run_simulate()
                    return
                self._connect_and_read()
                # clear last error on successful run
                self._last_error = None
            except Exception as exc:
                # record the error and retry after backoff (no stack trace)
                self._last_error = str(exc)
                self._connected = False
                # transient connection errors are expected; log at DEBUG unless verbose
                log.debug("monitor connection error: %s", exc)
            # backoff before reconnecting
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _connect_and_read(self) -> None:
        log.debug("connecting to %s:%s", self.host, self.port)
        # connection inactivity timeout derived from poll interval
        connection_timeout = self._poll_interval * 10.0
        with socket.create_connection((self.host, self.port), timeout=min(10.0, connection_timeout)) as s:
            self._sock = s
            self._connected = True
            self._last_error = None
            # set last message time to now; used to detect idle connections
            self._last_msg_ts = time.time()
            s.settimeout(self._poll_interval)
            log.debug("connected to %s:%s", self.host, self.port)
            # request initial SOC and battery temp (CMD2) asynchronously so UI updates quickly
            try:
                self._request_initial_attrs_async()
            except Exception as exc:
                log.debug("initial attrs request scheduling failed: %s", exc)
            # send an automatic CMD0 after a short delay so device sends network info
            def _do_send_cmd0():
                # This helper sends a CMD0 to request network information.
                # We check `_connected` (a best-effort flag) because the reader
                # thread may have already observed a disconnect and we don't want
                # to attempt a send on a closed socket.
                if not self._connected:
                    return
                try:
                    # ask device for CMD0; include an empty `msg` as required by the protocol
                    self.send_command(0, payload={"pv": 0, "msg": {}}, timeout=2.0)
                except TimeoutError:
                    # Expected if device is slow; will retry via polling or device may send unsolicited
                    pass
                except Exception as exc:
                    # Non-fatal: log only at debug so UIs can stay quiet unless verbose logging is requested
                    log.debug("auto CMD0 send failed: %s", exc)

            timer = threading.Timer(1.0, _do_send_cmd0)
            timer.daemon = True
            timer.start()
            try:
                recv_buf = b''
                while not self._stop_event.is_set():
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        # no data arrived; consider whether the connection has been idle too long
                        now = time.time()
                        if self._last_msg_ts is not None and (now - self._last_msg_ts) > connection_timeout:
                            log.debug("connection inactive for %.1f seconds, closing", (now - self._last_msg_ts))
                            self._last_error = "connection inactive timeout"
                            break
                        # otherwise continue waiting for data
                        continue
                    except OSError as exc:
                        # socket-level errors (e.g., connection reset) should cause reconnect
                        log.debug("socket error while reading: %s", exc)
                        self._last_error = str(exc)
                        break

                    if not chunk:
                        # connection closed
                        log.debug("connection closed by peer")
                        self._last_error = "connection closed by peer"
                        break

                    recv_buf += chunk
                    while b'\n' in recv_buf:
                        line_bytes, recv_buf = recv_buf.split(b'\n', 1)
                        try:
                            line = line_bytes.decode('utf-8')
                        except Exception as exc:
                            log.debug("failed to decode line bytes: %s", exc)
                            continue
                        try:
                            msg = parse_line(line)
                        except ValueError as exc:
                            # parsing noisy lines is expected; log at DEBUG
                            log.debug("failed to parse line: %s", exc)
                            continue
                        # update last message time and handle message
                        self._last_msg_ts = time.time()
                        self._handle_msg(msg)
            finally:
                # cancel the scheduled CMD0 if the connection closes before it's sent
                try:
                    timer.cancel()
                except Exception:
                    pass
                self._connected = False
                self._sock = None

    def _run_simulate(self) -> None:
        log.debug("simulate mode: reading %s", self.simulate_file)
        with open(self.simulate_file, "r", encoding="utf-8") as fh:
            for line in fh:
                if self._stop_event.is_set():
                    break
                try:
                    msg = parse_line(line)
                except ValueError:
                    continue
                self._handle_msg(msg)
                time.sleep(0.05)

    def _request_initial_attrs_async(self, timeout: Optional[float] = None) -> None:
        """Request initial attributes (SOC & battery temp) asynchronously.

        This sends a CMD2 for attrs [3,32] without blocking the reader thread.
        Timeouts are expected and silently ignored since responses arrive asynchronously.
        """
        def _fn():
            try:
                # Send an initial CMD2 request asynchronously and return; the
                # reader thread will process the response whenever it arrives.
                self.send_command(2, payload={"pv": 0, "msg": {"attr": [3, 32]}}, wait=False)
            except Exception as exc:
                log.debug("initial attrs CMD2 fire-and-forget failed: %s", exc)
        th = threading.Thread(target=_fn, daemon=True)
        th.start()

    DERIVED_SOLAR_ID = 100

    def _handle_msg(self, msg: Dict[str, Any]) -> None:
        cmd = int(msg.get("cmd", -1))
        sn = msg.get("sn")
        if cmd == 0:
            # CMD0: network info; only keep first seen
            with self._lock:
                if self._cmd0_info is None:
                    self._cmd0_info = msg.get("msg", {})
                    log.debug("stored CMD0 network info: %s", self._cmd0_info)
                    # request immediate SOC and battery temperature via CMD2
                    try:
                        self._request_initial_attrs_async()
                    except Exception as exc:
                        log.debug("request initial attrs on CMD0 failed: %s", exc)
        elif cmd in (10, 2):
            # CMD10 contains attribute data to be consumed directly. CMD2 is a
            # request/response that returns the same data format but includes a
            # `res` status field indicating whether the data is valid. Only
            # process CMD2 responses when `res == 0`.
            if cmd == 2:
                try:
                    res = int(msg.get('res', 1))
                except Exception:
                    res = 1
                if res != 0:
                    log.debug("ignoring CMD2 response with res=%s", res)
                    # Ensure any waiting sender for this sn is notified with the raw
                    # response so callers (e.g., set_relays) can inspect `res` and
                    # decide (abort vs fallback). Then set update event for UIs.
                    if sn is not None:
                        with self._outstanding_lock:
                            if sn in self._outstanding:
                                ev, store = self._outstanding[sn]
                                store['response'] = msg
                                ev.set()
                    try:
                        self._update_event.set()
                    except Exception:
                        pass
                    return
            parsed = process_cmd10(msg.get("msg", {}))
            ts = time.time()
            with self._lock:
                for aid, val in parsed.items():
                    self._attrs[aid] = {"value": val, "ts": ts}
                # Recompute derived values (e.g., solar: attr21 - attr22)
                self._recompute_derived_locked(ts)
        else:
            # For command responses (e.g., CMD3), handle 'res' and match by sn
            if sn is not None:
                with self._outstanding_lock:
                    if sn in self._outstanding:
                        ev, store = self._outstanding[sn]
                        store['response'] = msg
                        ev.set()
        # Notify any UI/clients waiting for updates that new data has arrived.
        try:
            self._update_event.set()
        except Exception:
            pass

    def _poll_loop(self) -> None:
        """Background poller that sends CMD2 requests in groups to actively
        request attribute data from the UPS without flooding the network.

        - Frequent group (every poll): [1,5,6,7,8,21,22,30]
        - Slow group (every 10 polls): [3,32]

        Requests are sent asynchronously; responses are processed by the normal
        reader (`_handle_msg`) when they arrive and only applied when `res == 0`.
        """
        slow_group = [3, 32]
        fast_group = [1, 5, 6, 7, 8, 21, 22, 30]
        counter = 0
        while not self._stop_event.is_set() and not self._poll_stop_event.is_set():
            # only poll when connected
            if not self._connected:
                # small sleep to avoid busy loop
                time.sleep(0.2)
                continue
            counter += 1
            # send frequent group asynchronously (don't wait for response)
            try:
                # Fire-and-forget the fast group poll; responses will be handled
                # asynchronously by the reader thread when they arrive.
                self.send_command(2, payload={"pv": 0, "msg": {"attr": fast_group}}, wait=False)
            except Exception as exc:
                log.debug("CMD2 fast group send failed: %s", exc)

            # send slow group every 10 polls
            if counter % 10 == 0:
                try:
                    self.send_command(2, payload={"pv": 0, "msg": {"attr": slow_group}}, wait=False)
                except Exception as exc:
                    log.debug("CMD2 slow group send failed: %s", exc)

            # sleep until next poll or stop event
            try:
                # allow quick wakeup when stopping
                if self._poll_stop_event.wait(self._poll_interval):
                    break
            except Exception:
                break

    def _recompute_derived_locked(self, ts: float) -> None:
        """Recompute derived attributes. Must be called with self._lock held."""
        # solar-only input = attr21 - attr22 (both in watts)
        v21 = self._attrs.get(21)
        v22 = self._attrs.get(22)
        if v21 is not None or v22 is not None:
            val21 = v21['value'] if v21 is not None else 0
            val22 = v22['value'] if v22 is not None else 0
            try:
                solar = int(val21) - int(val22)
            except Exception:
                solar = None
            self._attrs[self.DERIVED_SOLAR_ID] = {"value": solar, "ts": ts}
        else:
            # remove derived if neither present
            self._attrs.pop(self.DERIVED_SOLAR_ID, None)

    def get_solar_input(self) -> Optional[int]:
        """Convenience getter for the derived solar-only input (watts)."""
        return self.get_attr(self.DERIVED_SOLAR_ID)

    def get_remaining_runtime_minutes(self) -> Optional[int]:
        """Convenience getter for remaining runtime in minutes (attr 30)."""
        return self.get_attr(30)

    def set_relays(self, mask: int, values: int, timeout: float = 5.0) -> bool:
        """Set relay bits using CMD3.

        Parameters:
        - mask: bits that will be affected (1 = affected)
        - values: desired bit values (only bits in mask are used)

        Returns True on success (res == 0), False on failure (res != 0).

        This method reads the current relay state (attr 1 raw) and will leave
        unaffected bits untouched. If the relay state is unknown, it will attempt
        to fetch it before proceeding. If the state cannot be determined, the
        command is refused to avoid unsafe bit manipulation.
        """
        # read current relay raw value
        cur_attr = self.get_attr(1)
        
        # If not cached, try to fetch it with a reasonable timeout
        if cur_attr is None:
            try:
                parsed = self.get_attrs([1], timeout=2.0)
                if parsed and 1 in parsed:
                    cur_attr = parsed[1]
            except Exception as exc:
                log.debug("set_relays: failed to fetch relay state: %s", exc)
        
        # If still None, refuse the command (don't assume cur=0)
        if cur_attr is None:
            log.warning("set_relays: cannot determine current relay state; refusing to set relays")
            return False
        
        # Extract current raw value safely
        cur = 0
        if isinstance(cur_attr, dict):
            cur = int(cur_attr.get('raw', 0))
        elif isinstance(cur_attr, int):
            cur = cur_attr

        # compute new relay raw value
        new_raw = (cur & ~mask) | (values & mask)

        payload = {"pv": 0, "msg": {"attr": [1], "data": {"1": new_raw}}}
        try:
            resp = self.send_command(3, payload=payload, timeout=timeout)
        except Exception as exc:
            # Send failures are usually transient/timeouts; only log at DEBUG so the
            # message follows the dashboard's --verbose behavior (package logger
            # is WARNING by default unless --verbose is set).
            log.debug("set_relays send failed: %s", exc)
            return False
        if not resp:
            return False
        # response includes 'res' field (0 success, 1 fail). If missing, treat as failure.
        try:
            res = int(resp.get('res', 1))
        except Exception:
            return False

        if res != 0:
            return False

        # The device typically sends an immediate CMD10 after a successful CMD3
        # with updated attribute 1 (relay raw). Wait a short time for that update
        # to be processed so callers see the new relay state reliably.
        wait_deadline = time.time() + min(timeout, 1.0)
        while time.time() < wait_deadline:
            cur_attr = self.get_attr(1)
            cur_raw = None
            if isinstance(cur_attr, dict):
                cur_raw = int(cur_attr.get('raw', 0))
            elif isinstance(cur_attr, int):
                cur_raw = cur_attr
            if cur_raw == new_raw:
                return True
            time.sleep(0.02)
        # timed out waiting for attr update; still consider the command successful
        log.debug("set_relays: timed out waiting for attr 1 update; res==0")
        return True

    # Public getters
    def get_cmd0_info(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._cmd0_info) if self._cmd0_info is not None else None

    def get_attr(self, attr_id: int) -> Optional[Any]:
        with self._lock:
            v = self._attrs.get(attr_id)
            return None if v is None else v['value']

    def get_all(self) -> Dict[int, Any]:
        with self._lock:
            return {k: v['value'] for k, v in self._attrs.items()}

    def get_connection_status(self) -> Dict[str, Optional[str]]:
        """Return connection status with `connected` and optional `last_error`."""
        with self._lock:
            return {"connected": self._connected, "last_error": self._last_error}

    # Helpers for attribute requests
    def get_attrs(self, attrs: list[int], timeout: float = 2.0, log_timeout: bool = True) -> Optional[Dict[int, Any]]:
        """Explicitly request attributes using CMD2 and return parsed values.

        - Sends CMD2 with the requested `attr` array and waits up to `timeout` seconds
          for a response.
        - Only accepts responses with `res == 0`. If `res != 0` the data is
          ignored and None is returned.
        - On success, updates the monitor's internal attributes and returns the
          parsed mapping attr_id -> converted_value.
        - Returns None when no valid data is obtained (timeout or res != 0).
        - If log_timeout=False, suppresses debug logging of timeouts (useful for
          initialization requests where timeouts are expected).
        """
        if not isinstance(attrs, (list, tuple)):
            raise ValueError("attrs must be a list of attribute ids")
        payload = {"pv": 0, "msg": {"attr": list(attrs)}}
        try:
            resp = self.send_command(2, payload=payload, timeout=timeout)
        except TimeoutError:
            if log_timeout:
                log.debug("get_attrs: no reply for attrs=%s within %.2fs", attrs, timeout)
            return None
        except Exception as exc:
            log.debug("get_attrs: send failed for attrs=%s: %s", attrs, exc)
            return None

        if not resp:
            log.debug("get_attrs: empty response for attrs=%s", attrs)
            return None
        try:
            res = int(resp.get('res', 1))
        except Exception:
            res = 1
        if res != 0:
            log.debug("get_attrs: response res=%s for attrs=%s; ignoring", res, attrs)
            return None
        # parse and update internal state
        parsed = process_cmd10(resp.get('msg', {}))
        ts = time.time()
        with self._lock:
            for aid, val in parsed.items():
                self._attrs[aid] = {"value": val, "ts": ts}
            self._recompute_derived_locked(ts)
        try:
            self._update_event.set()
        except Exception:
            pass
        return parsed

    # Update notification helpers
    def wait_for_update(self, timeout: Optional[float] = None) -> bool:
        """Block until new data arrives or the optional timeout elapses.

        Returns True if an update was received, False on timeout.
        """
        return self._update_event.wait(timeout)

    def clear_update(self) -> None:
        """Clear the internal update event. Call after processing an update."""
        try:
            self._update_event.clear()
        except Exception:
            pass

    # Command send/response matching
    def send_command(self, cmd: int, payload: Optional[Dict[str, Any]] = None, timeout: float = 5.0, wait: bool = True) -> Optional[Dict[str, Any]]:
        """Send a command (JSON) to the device.

        - If ``wait`` is True (default) this blocks up to ``timeout`` seconds
          waiting for a response matched by SN and returns the response dict
          (or raises TimeoutError).
        - If ``wait`` is False the command is sent and the function returns the
          generated SN string on success (responses will be processed
          asynchronously by the reader thread).
        """
        if payload is None:
            payload = {}
        # ensure sn
        sn = payload.get("sn")
        if not sn:
            sn = str(int(time.time() * 1000))
            payload["sn"] = sn
        payload["cmd"] = cmd
        data = json.dumps(payload) + "\n"

        if not wait:
            # Fire-and-forget: just send the bytes and return sn (or None on error)
            try:
                with self._lock:
                    if not self._sock:
                        raise RuntimeError("not connected")
                    self._sock.sendall(data.encode("utf-8"))
                return sn
            except Exception as exc:
                log.debug("send_command (nowait) send failed: %s", exc)
                return None

        # Blocking path: register outstanding and wait for response
        ev = threading.Event()
        store: Dict[str, Any] = {}
        with self._outstanding_lock:
            self._outstanding[sn] = (ev, store)

        try:
            with self._lock:
                if not self._sock:
                    raise RuntimeError("not connected")
                self._sock.sendall(data.encode("utf-8"))
            ok = ev.wait(timeout=timeout)
            if not ok:
                raise TimeoutError("no response for sn=%s" % sn)
            return store.get('response')
        finally:
            with self._outstanding_lock:
                self._outstanding.pop(sn, None)

    # Note: send_command_nowait removed; use send_command(..., wait=False)
